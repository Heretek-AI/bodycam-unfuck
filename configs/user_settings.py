import os

user_settings = {}

if os.environ.get("SteamGameId") == "2406770":
    # Prevent external Vulkan layer hooks from conflicting with native Streamline Reflex
    os.environ.pop("LOW_LATENCY_LAYER", None)
    os.environ.pop("PROTON_ENABLE_WAYLAND", None)
    user_settings.update({
        "PROTON_FORCE_NVAPI": "1",
        "DXVK_ENABLE_NVAPI": "1",
        "VKD3D_CONFIG": "nodxr,no_upload_hvv",
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
