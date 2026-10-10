# Building SGUMI
All commands run from the **repository root**, not from `sgumi/`.

SDL, Dear ImGui, nlohmann/json and stb are vendored as submodules under `sgumi/third_party/` and need nothing installed. libcurl is the one dependency that comes from the system, plus a compiler, CMake, and whatever SDL needs to open a window on your platform.

## Dependencies
### Alpine
```sh
doas apk add build-base cmake git pkgconf curl-dev \
    mesa-dev mesa-vulkan-ati mesa-vulkan-intel vulkan-loader \
    libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxi-dev libxfixes-dev \
    libxkbcommon-dev wayland-dev
```

`mesa-vulkan-*` is per-GPU — install the one matching the laptop, or `mesa-vulkan-swrast` for software rendering, which is fine for a UI this static.

### macOS (brew)
```sh
brew install cmake
```

curl and the frameworks ship with the OS. Xcode Command Line Tools supply the compiler (`xcode-select --install`).

### Ubuntu / Debian (apt)
```sh
sudo apt install build-essential cmake git pkg-config libcurl4-openssl-dev \
    mesa-vulkan-drivers \
    libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxi-dev libxfixes-dev \
    libxkbcommon-dev libwayland-dev libdecor-0-dev libdrm-dev libgbm-dev
```

### Fedora (dnf)
```sh
sudo dnf install @development-tools cmake git pkgconf libcurl-devel \
    mesa-vulkan-drivers \
    libX11-devel libXext-devel libXrandr-devel libXcursor-devel libXi-devel \
    libXfixes-devel libxkbcommon-devel wayland-devel libdecor-devel \
    libdrm-devel mesa-libgbm-devel
```

### Windows (winget + vcpkg), from an administrator PowerShell
```
winget install Git.Git
winget install Microsoft.VisualStudio.2022.BuildTools --override "--wait --quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

CMake comes with the Visual Studio C++ workload. libcurl comes from vcpkg:

```
git clone https://github.com/microsoft/vcpkg C:\vcpkg
C:\vcpkg\bootstrap-vcpkg.bat
C:\vcpkg\vcpkg install curl:x64-windows
```

Then configure with the toolchain file:

```
cmake -S sgumi -B sgumi/build -DCMAKE_TOOLCHAIN_FILE=C:\vcpkg\scripts\buildsystems\vcpkg.cmake
```

## Building

```sh
git submodule update --init --recursive
cmake -S sgumi -B sgumi/build
cmake --build sgumi/build -j
```

`CMAKE_BUILD_TYPE` defaults to `RelWithDebInfo` rather than being left empty, which would mean `-O0` for SDL and ImGui as well as for our own code. Override it normally:

```sh
cmake -S sgumi -B sgumi/build -DCMAKE_BUILD_TYPE=Debug
```

Windows uses a multi-config generator, so the build type goes on the build step instead and the binary lands in a per-config subdirectory:

```
cmake --build sgumi/build --config Release
```

## Configure options

| Option | Default | What it does |
|---|---|---|
| `SGUMI_THEME_EDITOR` | `ON` | Compiles in the F10 palette editor. It starts hidden and costs nothing at runtime; turn it off for a build stripped of dev tooling |
| `SGUMI_ICON_NAME` | `SGUMI` | Which icon set under the repository root's `assets/` to build with. Every icon step is skipped, with a note, when the files are absent |

## The macOS icon and Xcode

macOS picks one of two icon pipelines at configure time, and says which:

```
-- Icon: actool, from SGUMI.icon
-- Icon: no actool, falling back to SGUMI.png
```

`actool` compiles the layered Icon Composer source into an `Assets.car` and is the only way to get the macOS 26+ layered icon. It ships **inside Xcode**, not the Command Line Tools, so a default `xcode-select -p` pointing at `/Library/Developer/CommandLineTools` takes the fallback.

If Xcode is installed but not selected, set `DEVELOPER_DIR` for the configure and build rather than switching your global `xcode-select`.

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cmake -S sgumi -B sgumi/build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer cmake --build sgumi/build -j
```

## Installing

```sh
cmake --install sgumi/build --prefix ~/.local
```

On Linux that puts `sgumi` in `bin/`, a `.desktop` file in `share/applications/` and, if an icon exists, the PNG into the hicolor icon theme, which is what gives the window a name and an icon in the xfce taskbar rather than a generic placeholder.

## Troubleshooting

**Configure stops with "third_party/sdl is empty."** The submodules are not checked out. `git submodule update --init --recursive`.

**Link fails with `_CFRelease` / `_CTFontCreateUIFontForLanguage` undefined on macOS.** `Theme.cpp` asks CoreText where the system UI font is, so the build has to link CoreText *and* CoreFoundation. The top-level `CMakeLists.txt` does this in its `if(APPLE)` block, check it survived an edit.

**`SDL_CreateGPUDevice failed`.** No usable GPU backend. On Linux this is almost always a missing Vulkan driver: install the `mesa-vulkan-*` matching the GPU, or `mesa-vulkan-swrast` to rule the driver out entirely.

**The window opens but says "lipgloss unreachable".** That is the app working and the service not. Check the URL in **SGUMI → Settings**, then `curl http://<host>:8081/health` from the same machine, `/health` needs no token, so it answers if anything is listening at all.
