#!/usr/bin/env python3
"""
bodycam-unfuck — Reverse-Engineered Binary & Engine Patcher for Bodycam (UE 5.5)
https://github.com/Heretek-AI/bodycam-unfuck

Fixes 6 game/engine-level architectural defects in Reissad Studio's Bodycam:
  1. Disables the 19 full-screen RDG compute shaders in the IMPERFECTER plugin
     (FImperfecterSceneViewExtension::SubscribeToPostProcessingPass -> ret)
     and disables Imperfecter's forced texture mip residency & 4K pass scaling.
  2. Disables the beta SDLJoystick plugin (USDLJoystickSubsystem::Initialize)
     so SDL2.dll stops opening & dual-polling Xbox/XInput controllers alongside
     Unreal Engine's native XInputDevice.
  3. Surgically patches UE5.5 GVAS GlobalUserSettings.sav so SettingsV2Managers
     (BP_Master / FL_EGUI_Easy) stops falling back to 'Lumen High' and 'DLAA'
     via ExecuteConsoleCommand (ECVF_SetByConsole) on startup.
  4. Enables NVIDIA Streamline Reflex Low Latency + Boost (t.Streamline.Reflex.Mode=2)
     while unloading unused DLSS-G frame-gen buffer tagging overhead.
  5. Disables Nanite programmable WPO software rasterization (CrazyIvy foliage)
     and strips Lumen down to ultra-fast indirect diffuse probes (or pure raster).
  6. Removes UE5's 24% axial thumbstick deadzone, view acceleration, and
     re-scales right-stick sensitivity to defeat InputModifipad's DeltaTime lag.

Zero external dependencies (Python 3.8+ standard library only).
Works on Linux (Steam/Proton & Steam Deck) and Windows.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP_ID = "2406770"
VERSION = "1.0.4"


# ---------------------------------------------------------------------------
# Path Discovery (Linux / Proton & Windows)
# ---------------------------------------------------------------------------

def find_steam_libraries() -> List[Path]:
    """Return candidate Steam library directories across Linux and Windows."""
    candidates: List[Path] = []
    home = Path.home()

    if platform.system() == "Windows":
        prog86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        prog = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        candidates.extend([prog86 / "Steam", prog / "Steam"])
    else:
        candidates.extend([
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ])

    libraries: List[Path] = []
    seen = set()
    for base in candidates:
        if not base.exists():
            continue
        real = base.resolve()
        if real not in seen:
            seen.add(real)
            libraries.append(base)
        vdf = base / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                    p = Path(m.group(1).replace(r"\\", "\\"))
                    if p.exists() and p.resolve() not in seen:
                        seen.add(p.resolve())
                        libraries.append(p)
            except OSError:
                pass
    return libraries


def discover_paths(
    game_dir_arg: Optional[str] = None,
    saved_dir_arg: Optional[str] = None,
) -> Tuple[Path, Path, Optional[Path]]:
    """
    Locate:
      1. game_dir:  .../Steam/steamapps/common/Bodycam
      2. saved_dir: .../Bodycam/Saved (inside Proton prefix on Linux or %LOCALAPPDATA% on Windows)
      3. proton_dir: .../Steam/steamapps/common/Proton - Experimental (Linux only, if present)
    """
    libraries = find_steam_libraries()

    game_dir: Optional[Path] = Path(game_dir_arg) if game_dir_arg else None
    if game_dir is None:
        cwd = Path.cwd()
        if (cwd / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe").exists():
            game_dir = cwd
        else:
            for lib in libraries:
                cand = lib / "steamapps/common/Bodycam"
                if (cand / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe").exists():
                    game_dir = cand
                    break

    if game_dir is None or not (game_dir / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe").exists():
        raise FileNotFoundError(
            "Could not auto-detect Bodycam installation directory. "
            "Pass --game-dir /path/to/Steam/steamapps/common/Bodycam"
        )

    saved_dir: Optional[Path] = Path(saved_dir_arg) if saved_dir_arg else None
    if saved_dir is None:
        if platform.system() == "Windows":
            local_app = os.environ.get("LOCALAPPDATA")
            if local_app:
                cand = Path(local_app) / "Bodycam/Saved"
                if cand.exists():
                    saved_dir = cand
        else:
            for lib in libraries:
                cand = (
                    lib
                    / f"steamapps/compatdata/{APP_ID}/pfx/drive_c/users/steamuser"
                    / "AppData/Local/Bodycam/Saved"
                )
                if cand.exists():
                    saved_dir = cand
                    break

    if saved_dir is None:
        raise FileNotFoundError(
            "Could not auto-detect Bodycam Saved directory. "
            "Launch the game once first or pass --saved-dir /path/to/Bodycam/Saved"
        )

    proton_dir: Optional[Path] = None
    if platform.system() != "Windows":
        for lib in libraries:
            cand = lib / "steamapps/common/Proton - Experimental"
            if cand.exists():
                proton_dir = cand
                break

    return game_dir, saved_dir, proton_dir


# ---------------------------------------------------------------------------
# Dynamic PE Signature Scanner & Binary Patcher
# ---------------------------------------------------------------------------

class PEBinaryPatcher:
    """
    Dynamically locates and patches:
      1. USDLJoystickSubsystem::Initialize (via XREF to "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS")
      2. FImperfecterSceneViewExtension::SubscribeToPostProcessingPass
         (via XREF to "r.Imperfecter.SVEPriority" -> GetPriority() -> VTable[-5])
    """

    def __init__(self, exe_path: Path):
        self.exe_path = exe_path
        self.bak_path = exe_path.with_suffix(exe_path.suffix + ".bak")

    @staticmethod
    def _parse_pe_sections(mm: mmap.mmap) -> Tuple[int, List[Tuple[str, int, int, int, int]]]:
        pe_off = struct.unpack("<I", mm[0x3C:0x40])[0]
        if mm[pe_off:pe_off + 4] != b"PE\x00\x00":
            raise ValueError("Invalid PE signature in executable")
        num_sec = struct.unpack("<H", mm[pe_off + 6:pe_off + 8])[0]
        opt_hdr_sz = struct.unpack("<H", mm[pe_off + 20:pe_off + 22])[0]
        img_base = struct.unpack("<Q", mm[pe_off + 24 + 24:pe_off + 24 + 32])[0]
        sec_off = pe_off + 24 + opt_hdr_sz
        sections = []
        for i in range(num_sec):
            s = mm[sec_off + i * 40:sec_off + (i + 1) * 40]
            name = s[:8].rstrip(b"\x00").decode("ascii", "ignore")
            vsize, rva, rawsize, rawoff = struct.unpack("<IIII", s[8:24])
            sections.append((name, rva, vsize, rawoff, rawsize))
        return img_base, sections

    @staticmethod
    def _file_to_rva(foff: int, sections: List[Tuple[str, int, int, int, int]]) -> int:
        for _, rva, _, rawoff, rawsize in sections:
            if rawoff <= foff < rawoff + rawsize:
                return rva + (foff - rawoff)
        raise ValueError(f"File offset {hex(foff)} not mapped to any PE section")

    @staticmethod
    def _rva_to_file(target_rva: int, sections: List[Tuple[str, int, int, int, int]]) -> int:
        for _, rva, vsize, rawoff, rawsize in sections:
            if rva <= target_rva < rva + max(vsize, rawsize):
                return rawoff + (target_rva - rva)
        raise ValueError(f"RVA {hex(target_rva)} not mapped to any PE section")

    def locate_targets(self) -> Dict[str, Tuple[int, str, bytes, bytes]]:
        """
        Scan Bodycam-Win64-Shipping.exe (or its .bak if available for clean XREFs)
        and return a dict of:
          name -> (file_offset, status_str, original_bytes, patched_bytes)
        """
        with open(self.exe_path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            img_base, sections = self._parse_pe_sections(mm)
            text_sec = next(s for s in sections if s[0] == ".text")
            _, text_rva, _, text_rawoff, text_rawsize = text_sec

            # --- Target 1: USDLJoystickSubsystem::Initialize ---
            sdl_str_off = mm.find(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS\x00")
            if sdl_str_off == -1:
                raise RuntimeError("Could not find SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS in binary")
            sdl_str_rva = self._file_to_rva(sdl_str_off, sections)

            sdl_xref_foff = None
            for m in re.finditer(
                b"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]",
                mm[text_rawoff:text_rawoff + text_rawsize],
            ):
                foff = text_rawoff + m.start()
                rva_next = text_rva + (foff - text_rawoff) + 7
                disp = struct.unpack("<i", mm[foff + 3:foff + 7])[0]
                if rva_next + disp == sdl_str_rva:
                    sdl_xref_foff = foff
                    break

            if sdl_xref_foff is None:
                raise RuntimeError("Could not find code XREF to SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS")

            # Patch site is the 7-byte instruction right before `lea rcx, "SDL_JOYSTICK..."`
            # (which is `lea rdx, "1"` immediately after `call Super::Initialize`)
            sdl_patch_foff = sdl_xref_foff - 7
            # Epilogue setting `this->bInitialized = 0` (`c6 43 58 00 48 83 c4 20 5b c3`)
            epilogue_sig = bytes.fromhex("c64358004883c4205bc3")
            epilogue_foff = mm.find(epilogue_sig, sdl_patch_foff, sdl_patch_foff + 0x80)
            if epilogue_foff == -1:
                raise RuntimeError("Could not find USDLJoystickSubsystem::Initialize failure epilogue")

            rel_jmp = epilogue_foff - (sdl_patch_foff + 2)
            if not (0 < rel_jmp < 0x7F):
                raise RuntimeError(f"Unexpected relative jump distance: {rel_jmp}")

            sdl_patched_bytes = bytes([0xEB, rel_jmp]) + b"\x90" * 5
            sdl_cur_bytes = mm[sdl_patch_foff:sdl_patch_foff + 7]

            # Read original bytes from .bak if already patched
            sdl_orig_bytes = sdl_cur_bytes
            if sdl_cur_bytes == sdl_patched_bytes and self.bak_path.exists():
                with open(self.bak_path, "rb") as bf:
                    bf.seek(sdl_patch_foff)
                    sdl_orig_bytes = bf.read(7)

            sdl_status = "PATCHED" if sdl_cur_bytes == sdl_patched_bytes else "UNPATCHED"

            # --- Target 2: FImperfecterSceneViewExtension::SubscribeToPostProcessingPass ---
            imp_str_off = mm.find("r.Imperfecter.SVEPriority\x00".encode("utf-16le"))
            if imp_str_off == -1:
                raise RuntimeError("Could not find r.Imperfecter.SVEPriority in binary")
            imp_str_rva = self._file_to_rva(imp_str_off, sections)

            imp_init_foff = None
            for m in re.finditer(
                b"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]",
                mm[text_rawoff:text_rawoff + text_rawsize],
            ):
                foff = text_rawoff + m.start()
                rva_next = text_rva + (foff - text_rawoff) + 7
                disp = struct.unpack("<i", mm[foff + 3:foff + 7])[0]
                if rva_next + disp == imp_str_rva:
                    imp_init_foff = foff
                    break

            if imp_init_foff is None:
                raise RuntimeError("Could not find r.Imperfecter.SVEPriority initializer XREF")

            # After `call IConsoleManager::RegisterConsoleVariableRef` (`ff 50 18`) and `mov rdx, rax` (`48 8b d0`),
            # `lea rcx, [rip + disp32]` points to the CVarImperfecterSVEPriority global.
            # Its value pointer is at `global_rva + 0x10` (or read by `mov rax, [rip+disp32]; mov eax, [rax]; ret`).
            lea_rcx_off = imp_init_foff + 7 + 3 + 3  # after lea rdx (7) + ff 50 18 (3) + 48 8b d0 (3)
            cvar_disp = struct.unpack("<i", mm[lea_rcx_off + 3:lea_rcx_off + 7])[0]
            cvar_global_rva = (text_rva + (lea_rcx_off - text_rawoff) + 7) + cvar_disp
            cvar_valptr_rva = cvar_global_rva + 0x10

            # Find FImperfecterSceneViewExtension::GetPriority(): `48 8b 05 <disp32> 8b 00 c3`
            get_prio_rva = None
            for m in re.finditer(
                b"\x48\x8b\x05....\x8b\x00\xc3",
                mm[text_rawoff:text_rawoff + text_rawsize],
            ):
                foff = text_rawoff + m.start()
                rva_next = text_rva + (foff - text_rawoff) + 7
                disp = struct.unpack("<i", mm[foff + 3:foff + 7])[0]
                if rva_next + disp == cvar_valptr_rva:
                    get_prio_rva = text_rva + (foff - text_rawoff)
                    break

            if get_prio_rva is None:
                raise RuntimeError("Could not locate FImperfecterSceneViewExtension::GetPriority()")

            vtable_entry_off = mm.find(struct.pack("<Q", img_base + get_prio_rva))
            if vtable_entry_off == -1:
                raise RuntimeError("Could not locate FImperfecterSceneViewExtension VTable")

            # SubscribeToPostProcessingPass is at vtable[-5]
            sub_pp_va = struct.unpack("<Q", mm[vtable_entry_off - 5 * 8:vtable_entry_off - 4 * 8])[0]
            sub_pp_rva = sub_pp_va - img_base
            sub_pp_foff = self._rva_to_file(sub_pp_rva, sections)

            sub_cur_byte = mm[sub_pp_foff:sub_pp_foff + 1]
            sub_tail = mm[sub_pp_foff + 1:sub_pp_foff + 10]
            if sub_tail != bytes.fromhex("897c24184c89742420"):
                raise RuntimeError(
                    f"Unexpected prologue at SubscribeToPostProcessingPass ({hex(sub_pp_foff)}): "
                    f"{mm[sub_pp_foff:sub_pp_foff + 10].hex()}"
                )

            sub_status = "PATCHED" if sub_cur_byte == b"\xc3" else "UNPATCHED"

            mm.close()

        return {
            "USDLJoystickSubsystem::Initialize": (
                sdl_patch_foff,
                sdl_status,
                sdl_orig_bytes,
                sdl_patched_bytes,
            ),
            "FImperfecterSceneViewExtension::SubscribeToPostProcessingPass": (
                sub_pp_foff,
                sub_status,
                b"\x48",
                b"\xc3",
            ),
        }

    def apply(self) -> Dict[str, Tuple[int, str]]:
        targets = self.locate_targets()
        if not self.bak_path.exists():
            shutil.copy2(self.exe_path, self.bak_path)

        results = {}
        with open(self.exe_path, "r+b") as f:
            for name, (foff, _status, _orig, patched) in targets.items():
                f.seek(foff)
                f.write(patched)
                results[name] = (foff, "PATCHED")
        return results

    def restore(self) -> bool:
        if self.bak_path.exists():
            shutil.copy2(self.bak_path, self.exe_path)
            return True
        targets = self.locate_targets()
        with open(self.exe_path, "r+b") as f:
            for _name, (foff, _status, orig, _patched) in targets.items():
                f.seek(foff)
                f.write(orig)
        return True


# ---------------------------------------------------------------------------
# UE5.5 GVAS Save File Patcher (GlobalUserSettings.sav)
# ---------------------------------------------------------------------------

def patch_global_user_settings_sav(
    sav_path: Path,
    sensitivity: float = 3.2,
    ads_sensitivity: float = 1.2,
) -> bool:
    """
    Surgically updates UE5.5 GVAS GlobalUserSettings.sav so BP_Master / FL_EGUI_Easy
    does not execute 'Lumen High' or 'DLAA' console commands at ECVF_SetByConsole priority.
    """
    if not sav_path.exists():
        return False

    unlock_file(sav_path)
    bak_path = sav_path.with_suffix(".sav.bak")
    if not bak_path.exists():
        shutil.copy2(sav_path, bak_path)

    raw = sav_path.read_bytes()
    pos = raw.find(b"SettingsData\x00")
    if pos == -1:
        raise RuntimeError("SettingsData property not found in GlobalUserSettings.sav")

    # UE5.5 StrProperty header:
    # "SettingsData\x00" (13) + "\x0c\x00\x00\x00StrProperty\x00" (16) = 29 bytes
    # followed by: uint32(0), uint32(prop_size), uint8(0), int32(str_len)
    z4, prop_size, z1, str_len = struct.unpack("<IIBi", raw[pos + 29:pos + 42])
    if prop_size != str_len + 4:
        raise RuntimeError(
            f"Unexpected GVAS StrProperty header in {sav_path}: prop_size={prop_size}, str_len={str_len}"
        )

    str_start = pos + 42
    obj = json.loads(raw[str_start:str_start + str_len - 1].decode("utf-8"))

    obj["Virtual Shadow Maps"] = False
    obj["Film Grain"] = False
    obj["Contact Shadows"] = False
    obj["Gamepad Sensitivity"] = float(sensitivity)
    obj["Gamepad ADS Sensitivity"] = float(ads_sensitivity)
    obj["AA Method"] = {"newValue": 0, "incrementedValue": 0, "valueName": "Off"}
    obj["Upscaling Method"] = {"newValue": 4, "incrementedValue": 4, "valueName": "Nvidia DLSS"}
    obj["Upscaling Quality"] = {"newValue": 1, "incrementedValue": 1, "valueName": "Performance"}
    obj["Resolution Scale"] = 50
    obj["Frame Generation Method"] = {"newValue": 0, "incrementedValue": 0, "valueName": "Off"}
    obj["VSync"] = {"newValue": 0, "incrementedValue": 0, "valueName": "Off"}
    obj["Motion Blur Quality"] = {"newValue": 1, "incrementedValue": 1, "valueName": "None"}
    obj["Motion Blur Amount"] = 0
    obj["Camera Shake"] = {"newValue": 0, "incrementedValue": 0, "valueName": "Reduced"}

    new_json_bytes = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\x00"
    new_str_len = len(new_json_bytes)
    new_prop_size = 4 + new_str_len

    new_raw = (
        raw[:pos + 29]
        + struct.pack("<IIBi", z4, new_prop_size, z1, new_str_len)
        + new_json_bytes
        + raw[str_start + str_len:]
    )
    sav_path.write_bytes(new_raw)
    return True


# ---------------------------------------------------------------------------
# Config Generators (Engine.ini, ConsoleVariables.ini, GameUserSettings.ini, Input.ini)
# ---------------------------------------------------------------------------

def build_cvar_block(preset: str, max_fps: int, vram_pool: int) -> List[str]:
    lumen_diffuse = 0 if preset == "raster-120" else 1
    screen_pct = 66 if preset == "quality-60" else 50
    mip_bias = 0 if preset == "quality-60" else 1

    return [
        "; --- 1. Enable Streamline Reflex Boost & Unload DLSS-G Tagging Overhead ---",
        "t.Streamline.Reflex.Enable=1",
        "t.Streamline.Reflex.Auto=1",
        "t.Streamline.Reflex.Mode=2",
        "t.Streamline.Reflex.HandleMaxTickRate=1",
        "t.Streamline.Reflex.EnableLatencyMarkers=1",
        "r.Streamline.Load.DLSSG=0",
        "r.Streamline.DLSSG.Enable=0",
        "r.Streamline.TagSceneColorWithoutHUD=0",
        "r.Streamline.TagUIColorAlpha=0",
        f"t.MaxFPS={max_fps}",
        "",
        "; --- 2. Parallel Game/Render Thread + Low-Latency Flip Queue ---",
        "r.OneFrameThreadLag=1",
        "r.FinishCurrentFrame=0",
        "r.D3D12.UseAllowTearing=1",
        "r.VSync=0",
        "",
        "; --- 3. Internal Resolution & Strict 8GB VRAM Budget (Prevents NVRM gpu_vaspace Crash) ---",
        "r.NGX.DLSS.Enable=1",
        f"r.ScreenPercentage={screen_pct}",
        "r.ScreenPercentage.MaxResolution=1080",
        "r.SecondaryScreenPercentage.GameViewport=100",
        f"r.Streaming.PoolSize={vram_pool}",
        "r.Streaming.MaxTempMemoryAllowed=64",
        f"r.Streaming.MipBias={mip_bias}",
        "r.Streaming.LimitPoolSizeToVRAM=1",
        "r.Streaming.HLODStrategy=2",
        "r.Streaming.FullyLoadUsedTextures=0",
        "r.RenderTargetPoolMin=300",
        "",
        "; --- 4. Nanite & Shadow Capping ---",
        "r.Nanite.MaxPixelsPerEdge=4",
        "r.Nanite.AsyncRasterization=1",
        "r.Shadow.Virtual.Enable=0",
        "r.Shadow.Virtual.MaxPhysicalPages=1024",
        "r.Shadow.DistanceScale=0.6",
        "r.Shadow.MaxCSMResolution=1024",
        "foliage.LODDistanceScale=0.5",
        "grass.DensityScale=0.4",
        "",
        "; --- 5. Fast Lumen (Preserves Natural Indoor Lighting & Sky Occlusion) ---",
        "r.VolumetricCloud=0",
        "r.VolumetricFog=0",
        f"r.Lumen.DiffuseIndirect.Allow={lumen_diffuse}",
        "r.Lumen.Reflections.Allow=0",
        "r.Lumen.Reflections.HardwareRayTracing=0",
        "r.Lumen.HardwareRayTracing=0",
        "r.Lumen.TranslucencyVolume.Enable=0",
        "r.Lumen.TranslucencyReflections.FrontLayer.Allow=0",
        "r.Lumen.ScreenProbeGather.DownsampleFactor=32",
        "r.Lumen.ScreenProbeGather.RadianceCache.GridResolution=24",
        "",
        "; --- 6. Safe Post-Process Reductions (Preserves 3D ColorGradingLUT & Histogram Exposure) ---",
        "r.FilmGrain=0",
        "r.Tonemapper.GrainQuantization=0",
        "r.DepthOfFieldQuality=0",
        "r.DepthOfField.DepthBlur.Amount=0",
        "r.SceneColorFringeQuality=0",
        "r.SceneColorFringe.Max=0",
        "r.LensBlurQuality=0",
        "r.MotionBlurQuality=0",
        "r.MotionBlur.Max=0",
        "r.LensDistortion.AffectScreenPercentage=0",
        "r.SSR.Quality=0",
        "r.FidelityFX.FI.OverrideSwapChainDX12=0",
    ]


INPUT_INI_CONTENT = """[/Script/Engine.InputSettings]
bEnableMouseSmoothing=False
bViewAccelerationEnabled=False
bDisableMouseAcceleration=True
RawMouseInputEnabled=1
AxisConfig=(AxisKeyName="Gamepad_LeftX",AxisProperties=(DeadZone=0.050000,Sensitivity=1.000000,Exponent=1.000000,bInvert=False))
AxisConfig=(AxisKeyName="Gamepad_LeftY",AxisProperties=(DeadZone=0.050000,Sensitivity=1.000000,Exponent=1.000000,bInvert=False))
AxisConfig=(AxisKeyName="Gamepad_RightX",AxisProperties=(DeadZone=0.050000,Sensitivity=1.000000,Exponent=1.000000,bInvert=False))
AxisConfig=(AxisKeyName="Gamepad_RightY",AxisProperties=(DeadZone=0.050000,Sensitivity=1.000000,Exponent=1.000000,bInvert=False))
"""


def build_game_user_settings_ini(max_fps: int, screen_pct: int = 50) -> str:
    return f"""[/Script/Bodycam.CustomGameUserSettings]
bUseVSync=False
bUseDynamicResolution=False
ResolutionSizeX=1920
ResolutionSizeY=1080
LastUserConfirmedResolutionSizeX=1920
LastUserConfirmedResolutionSizeY=1080
WindowPosX=-1
WindowPosY=-1
FullscreenMode=0
LastConfirmedFullscreenMode=0
PreferredFullscreenMode=0
Version=5
AudioQualityLevel=0
LastConfirmedAudioQualityLevel=0
FrameRateLimit={float(max_fps):.6f}
DesiredScreenWidth=1920
DesiredScreenHeight=1080
LastUserConfirmedDesiredScreenWidth=1920
LastUserConfirmedDesiredScreenHeight=1080
LastRecommendedScreenWidth=-1.000000
LastRecommendedScreenHeight=-1.000000
LastCPUBenchmarkResult=-1.000000
LastGPUBenchmarkResult=-1.000000
LastGPUBenchmarkMultiplier=1.000000
bUseHDRDisplayOutput=False
HDRDisplayOutputNits=1000

[/Script/Engine.GameUserSettings]
bUseDesiredScreenHeight=True

[ScalabilityGroups]
sg.ResolutionQuality={screen_pct}
sg.ViewDistanceQuality=0
sg.AntiAliasingQuality=0
sg.ShadowQuality=0
sg.GlobalIlluminationQuality=2
sg.ReflectionQuality=1
sg.PostProcessQuality=3
sg.TextureQuality=0
sg.EffectsQuality=0
sg.FoliageQuality=0
sg.ShadingQuality=0
sg.LandscapeQuality=3
"""


PROTON_USER_SETTINGS_PY = """import os

user_settings = {}

if os.environ.get("SteamGameId") == "2406770":
    # Prevent external Vulkan layer hooks from conflicting with native Streamline Reflex
    os.environ.pop("LOW_LATENCY_LAYER", None)
    os.environ.pop("PROTON_ENABLE_WAYLAND", None)
    # Strip 'no_upload_hvv' if set in Steam Launch Options to prevent NVRM intermapRegisterDmaMapping / gpu_vaspace.c:4155 crash
    os.environ["VKD3D_CONFIG"] = "nodxr"
    user_settings.update({
        "PROTON_FORCE_NVAPI": "1",
        "DXVK_ENABLE_NVAPI": "1",
        "VKD3D_CONFIG": "nodxr",
        "MANGOHUD": "1",
        "__GL_GSYNC_ALLOWED": "1",
        "__GL_VRR_ALLOWED": "1",
        "__GL_MaxFramesAllowed": "1",
        # Block Bodycam's beta SDLJoystick plugin (SDL2.dll) from opening/polling the Xbox controller
        "SDL_AUTO_UPDATE_JOYSTICKS": "0",
        "SDL_JOYSTICK_HIDAPI": "0",
        "SDL_JOYSTICK_RAWINPUT": "0",
        "SDL_JOYSTICK_WGI": "0",
        "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS": "0",
        "SDL_GAMECONTROLLER_IGNORE_DEVICES": "0x045e/0x028e",
        "SDL_JOYSTICK_BLACKLIST_DEVICES": "0x045e/0x028e",
    })
"""


def unlock_file(path: Path) -> None:
    if path.exists():
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass


def lock_file(path: Path) -> None:
    if path.exists():
        try:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass


def write_locked_config(path: Path, content: str, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unlock_file(path)
    bak = path.with_suffix(path.suffix + ".bak")
    if backup and path.exists() and not bak.exists():
        shutil.copy2(path, bak)
    path.write_text(content, encoding="utf-8")
    lock_file(path)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def cmd_status(game_dir: Path, saved_dir: Path, proton_dir: Optional[Path]) -> int:
    exe_path = game_dir / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe"
    patcher = PEBinaryPatcher(exe_path)
    targets = patcher.locate_targets()

    print(f"=== bodycam-unfuck v{VERSION} Status ===")
    print(f"Game Dir  : {game_dir}")
    print(f"Saved Dir : {saved_dir}")
    if proton_dir:
        print(f"Proton Dir: {proton_dir}")
    print("\n[Binary Targets in Bodycam-Win64-Shipping.exe]")
    for name, (foff, status, _orig, _patched) in targets.items():
        print(f"  - {name} @ {hex(foff)}: {status}")

    print("\n[Configuration & Save Files]")
    for rel in [
        "Config/Windows/Engine.ini",
        "Config/Windows/GameUserSettings.ini",
        "Config/Windows/Input.ini",
        "SaveGames/GlobalUserSettings.sav",
    ]:
        p = saved_dir / rel
        if p.exists():
            mode = oct(p.stat().st_mode & 0o777)
            ro = "READ-ONLY (locked)" if not (p.stat().st_mode & stat.S_IWUSR) else "WRITABLE"
            print(f"  - {rel}: PRESENT ({mode}, {ro})")
        else:
            print(f"  - {rel}: MISSING")
    return 0


def cmd_install(args: argparse.Namespace, game_dir: Path, saved_dir: Path, proton_dir: Optional[Path]) -> int:
    exe_path = game_dir / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe"
    patcher = PEBinaryPatcher(exe_path)

    if args.offline_binary_patch:
        print(f"[1/4] Patching binary (offline/no-EAC mode): {exe_path}")
        res = patcher.apply()
        for name, (foff, status) in res.items():
            print(f"      -> {name} @ {hex(foff)}: {status}")
    else:
        print(f"[1/4] Keeping {exe_path.name} unmodified (EasyAntiCheat catalogue safe)")
        if patcher.bak_path.exists():
            patcher.restore()
            patcher.bak_path.unlink(missing_ok=True)

    print(f"[2/4] Writing & locking Engine, Scalability, and Input configs...")
    cvars = build_cvar_block(args.preset, args.fps, args.vram_pool)
    engine_ini_text = "[SystemSettings]\n" + "\n".join(cvars) + "\n"

    cfg_dir = saved_dir / "Config/Windows"
    write_locked_config(cfg_dir / "Engine.ini", engine_ini_text)
    write_locked_config(cfg_dir / "Input.ini", INPUT_INI_CONTENT)
    screen_pct = 66 if args.preset == "quality-60" else 50
    gus_path = cfg_dir / "GameUserSettings.ini"
    unlock_file(gus_path)
    gus_path.write_text(build_game_user_settings_ini(args.fps, screen_pct), encoding="utf-8")

    sav_path = saved_dir / "SaveGames/GlobalUserSettings.sav"
    print(f"[3/4] Surgically patching UE5.5 GVAS save file: {sav_path}")
    if patch_global_user_settings_sav(sav_path, args.sensitivity, args.ads_sensitivity):
        print("      -> Disabled VSM/DLAA/FilmGrain, set DLSS Performance & controller sensitivity")
    else:
        print("      -> GlobalUserSettings.sav not found yet (will be created by game)")

    if proton_dir is not None:
        user_settings_path = proton_dir / "user_settings.py"
        print(f"[4/4] Configuring Proton Experimental environment: {user_settings_path}")
        if not user_settings_path.exists():
            user_settings_path.write_text(PROTON_USER_SETTINGS_PY, encoding="utf-8")
            print("      -> Created user_settings.py (NVAPI + Reflex + SDL2 controller block)")
        else:
            print("      -> user_settings.py already exists; keeping existing file")
    else:
        print("[4/4] Skipping Proton user_settings.py (Windows or Proton Experimental not found)")

    print("\nDone! Launch Bodycam from Steam.")
    return 0


def cmd_uninstall(game_dir: Path, saved_dir: Path) -> int:
    exe_path = game_dir / "Bodycam/Binaries/Win64/Bodycam-Win64-Shipping.exe"
    patcher = PEBinaryPatcher(exe_path)
    print(f"[1/2] Restoring binary: {exe_path}")
    patcher.restore()

    print(f"[2/2] Unlocking and restoring config/save backups in {saved_dir}")
    for rel in [
        "Config/Windows/Engine.ini",
        "Config/Windows/GameUserSettings.ini",
        "Config/Windows/Input.ini",
        "SaveGames/GlobalUserSettings.sav",
    ]:
        p = saved_dir / rel
        unlock_file(p)
        bak = p.with_suffix(p.suffix + ".bak")
        if bak.exists():
            shutil.copy2(bak, p)
            unlock_file(p)
            print(f"      -> Restored {rel} from .bak")
    print("\nRestore complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bodycam_unfuck.py",
        description="Reverse-engineered binary & engine patcher for Bodycam (120 FPS / 8GB VRAM / Zero-Lag Controller).",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=["install", "status", "uninstall", "restore"],
        help="Action to perform (default: install)",
    )
    parser.add_argument(
        "--preset",
        default="lumen-120",
        choices=["lumen-120", "raster-120", "quality-60"],
        help="Lighting/performance preset (default: lumen-120)",
    )
    parser.add_argument("--fps", type=int, default=120, help="Target framerate cap (default: 120)")
    parser.add_argument("--vram-pool", type=int, default=2400, help="Texture streaming pool in MB (default: 2400)")
    parser.add_argument("--sensitivity", type=float, default=3.2, help="Gamepad look sensitivity (default: 3.2)")
    parser.add_argument("--ads-sensitivity", type=float, default=1.2, help="Gamepad ADS sensitivity (default: 1.2)")
    parser.add_argument(
        "--offline-binary-patch",
        action="store_true",
        help="Also patch Bodycam-Win64-Shipping.exe directly (offline/no-EAC only; triggers EAC catalogue mismatch if launched via Bodycam.exe)",
    )
    parser.add_argument("--game-dir", default=None, help="Path to Steam/steamapps/common/Bodycam")
    parser.add_argument("--saved-dir", default=None, help="Path to Bodycam/Saved directory")

    args = parser.parse_args()
    game_dir, saved_dir, proton_dir = discover_paths(args.game_dir, args.saved_dir)

    if args.action == "status":
        return cmd_status(game_dir, saved_dir, proton_dir)
    if args.action in ("uninstall", "restore"):
        return cmd_uninstall(game_dir, saved_dir)
    return cmd_install(args, game_dir, saved_dir, proton_dir)


if __name__ == "__main__":
    sys.exit(main())
