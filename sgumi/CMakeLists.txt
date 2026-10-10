# The Super Graphic Ultra Modern Interface -- illusion's desktop frontend for
# lipgloss.
#
# A standalone CMake project rather than part of the uv workspace: the rest of
# illusion is Python and has no business knowing about SDL. Configure it on its
# own from the repository root:
#
#   cmake -S sgumi -B sgumi/build
#   cmake --build sgumi/build -j
#
# The window, the frame loop, the config-path resolution and the packaging
# rules below are ported from Eastern Edge's Software_2027 surface frontend at
# commit ff6a7f6, and then diverged. That is a fork point, not a subscription:
# nothing here tracks that repo automatically. To see what has changed in the
# bones since, run this over there --
#
#   git diff ff6a7f6..HEAD -- apps/frontend/src/main.cpp \
#                             apps/frontend/CMakeLists.txt \
#                             libs/eer_gfx/src/Theme.cpp
#
# -- and port by hand what is worth porting.

cmake_minimum_required(VERSION 3.25)

# Versioned in lockstep with the Python packages, the way docs/split-plan.md
# describes. Bump this when they bump, not on its own schedule.
project(sgumi VERSION 1.6.0 LANGUAGES C CXX)

# Without this, a bare `cmake -S sgumi -B sgumi/build` configures with an empty
# build type, which means no -O flag at all -- not "lightly optimised", but -O0
# for this project and for vendored ImGui and SDL with it.
#
# Only a default: -DCMAKE_BUILD_TYPE=Debug still gets you a Debug build.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING
    "Build type (Debug, Release, RelWithDebInfo, MinSizeRel)" FORCE)
  set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS
    Debug Release RelWithDebInfo MinSizeRel)
endif()

message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED YES)
set(CMAKE_CXX_EXTENSIONS NO)

# Applied per-target to our own code only. Vendored code in third_party/ builds
# with whatever its upstream chose, so a dependency bump cannot bury our
# warnings under thousands of theirs.
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  set(SGUMI_WARNINGS -Wall -Wextra -Wpedantic)
else()
  set(SGUMI_WARNINGS)
endif()

# A checkout with no submodules produces a wall of "no such file" from deep
# inside third_party/ that says nothing about the cause. Check the one file
# that is certain to exist and name the fix instead.
if(NOT EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/third_party/sdl/CMakeLists.txt)
  message(FATAL_ERROR
    "third_party/sdl is empty. Clone with submodules, or run:\n"
    "  git submodule update --init --recursive"
  )
endif()

# HTTP to lipgloss. Not vendored, unlike the four above: libcurl is the one
# dependency every platform here already ships or packages, and the same choice
# Software_2027 made for its WHEP posts. See docs/Building.md for how to get it
# on each platform.
find_package(CURL REQUIRED)

add_subdirectory(third_party)

add_executable(
  sgumi
  src/main.cpp
  src/Claws.cpp
  src/Http.cpp
  src/Image.cpp
  src/Lipgloss.cpp
  src/Paths.cpp
  src/Theme.cpp
)

# Build date, shown in the About window. The custom target runs on every build
# so the stamp cannot go stale, but the script only rewrites the header when the
# date changes -- so this costs a recompile once a day, not once a build.
# See cmake/write_build_info.cmake.
set(BUILD_INFO_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated)
file(MAKE_DIRECTORY ${BUILD_INFO_DIR})

add_custom_target(
  sgumi_build_info ALL
  COMMAND ${CMAKE_COMMAND}
    -DOUTPUT=${BUILD_INFO_DIR}/build_info.h
    -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/write_build_info.cmake
  COMMENT "Stamping build date"
)

add_dependencies(sgumi sgumi_build_info)

target_include_directories(
  sgumi
  PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${BUILD_INFO_DIR}
)

# Runtime palette editor, toggled with F10. Starts hidden, so leaving this on
# costs nothing at runtime; turn it off for a build you want stripped of dev
# tooling entirely.
option(SGUMI_THEME_EDITOR "Include the runtime theme editor (F10)" ON)

# The project version reaches the About window the same way the build date
# does, rather than being spelled out a second time in C++.
target_compile_definitions(
  sgumi
  PRIVATE
    SGUMI_VERSION="${PROJECT_VERSION}"
    $<$<BOOL:${SGUMI_THEME_EDITOR}>:SGUMI_THEME_EDITOR=1>
)

target_compile_options(sgumi PRIVATE ${SGUMI_WARNINGS})

target_link_libraries(
  sgumi
  PRIVATE
    sgumi::sdl3
    sgumi::imgui
    sgumi::stb
    nlohmann_json::nlohmann_json
    CURL::libcurl
)

# Theme.cpp asks CoreText where the system UI font is, rather than guessing at
# paths. A C API, so this needs the frameworks but not Objective-C++ --
# OBJCXX stays disabled and there is no .mm file in this project.
#
# CoreFoundation alongside CoreText because systemUiFontPath() does not only
# call CoreText: CFRelease and CFURLGetFileSystemRepresentation are
# CoreFoundation. Over in Software_2027 those two resolved by accident, through
# a "-framework Foundation" that apps/frontend adds for an unrelated
# Objective-C++ file -- macOS's Foundation re-exports CoreFoundation. There is
# no such accident here, so this link failed immediately on the first build:
#
#   Undefined symbols for architecture arm64:
#     "_CFRelease", referenced from: applyTheme() in Theme.cpp.o
#
# find_library rather than a bare "-framework CoreText" so the dependency is a
# resolved path, which survives being passed along as a link requirement.
if(APPLE)
  find_library(SGUMI_CORETEXT_LIBRARY CoreText REQUIRED)
  find_library(SGUMI_COREFOUNDATION_LIBRARY CoreFoundation REQUIRED)

  target_link_libraries(
    sgumi
    PRIVATE
      ${SGUMI_CORETEXT_LIBRARY}
      ${SGUMI_COREFOUNDATION_LIBRARY}
  )
endif()

# ---------------------------------------------------------------------------
# Packaging.
#
# Ported wholesale from Software_2027 -- this is the part that is genuinely
# cross-platform and genuinely tedious, and the reason the scaffold was worth
# taking at all. The icon handling differs in one way: over there a missing
# icon is a configure error, because that app always ships one. Here every
# icon step is gated on the file existing, so SGUMI still builds with no art
# at all -- useful on a machine that only ever runs it headless-ish from a
# terminal, and it is what let this project build before the icon existed.
#
# One source of truth per platform. The icon set lives at the repository root
# rather than under sgumi/, beside the Python packages' own assets, and every
# file inside repeats the set's name:
#
#   assets/${SGUMI_ICON_NAME}/${SGUMI_ICON_NAME}.icon
#                                  Apple. Icon Composer's format: a directory
#                                  of layers plus icon.json. actool renders
#                                  every size the OS wants, so no hand-exported
#                                  size set is committed anywhere.
#   assets/${SGUMI_ICON_NAME}/${SGUMI_ICON_NAME}.png
#                                  1024x1024. Linux, and the macOS fallback.
#   assets/${SGUMI_ICON_NAME}/${SGUMI_ICON_NAME}.ico
#                                  Windows, compiled into the .exe.
#
# The name repeats in the directory and the basenames rather than being folded
# into one because actool requires the app-icon name to match the .icon's
# basename -- the directory only groups a set, it cannot supply the name.
# ---------------------------------------------------------------------------
#
# Included here rather than at the bottom because the Linux branch below
# installs into CMAKE_INSTALL_DATAROOTDIR, which does not exist until this runs.
include(GNUInstallDirs)

set(SGUMI_ICON_NAME "SGUMI" CACHE STRING
  "Icon set under assets/ to build with")

# The repository root, not sgumi/ -- this project is configured as its own
# top-level CMake project, so CMAKE_SOURCE_DIR would point at sgumi/ too.
set(SGUMI_ASSETS_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../assets)
set(SGUMI_ICON_DIR ${SGUMI_ASSETS_DIR}/${SGUMI_ICON_NAME})
set(SGUMI_ICON_ICON ${SGUMI_ICON_DIR}/${SGUMI_ICON_NAME}.icon)
set(SGUMI_ICON_PNG ${SGUMI_ICON_DIR}/${SGUMI_ICON_NAME}.png)
set(SGUMI_ICON_ICO ${SGUMI_ICON_DIR}/${SGUMI_ICON_NAME}.ico)

if(APPLE)
  # A bundle rather than a bare Mach-O. Three things need it: the Dock reads
  # the name and icon from Info.plist, the window server treats a bundled
  # process as a real app for activation and focus, and a bundle is what
  # `open` and a Finder double-click can launch at all.
  #
  # The cost is that the binary moves to
  # sgumi.app/Contents/MacOS/sgumi. Running that path directly still behaves
  # exactly as before -- stdout to the terminal, working directory preserved --
  # which is why every command in the docs spells it out.
  set(SGUMI_ICON_OUT ${CMAKE_CURRENT_BINARY_DIR}/icon)

  # Which icon pipeline is available decides what goes in the bundle, so this
  # is resolved before the target properties below.
  #
  # actool is inside Xcode. sips and iconutil are in /usr/bin, part of macOS
  # itself, so the fallback works on any Mac -- including one with only the
  # Command Line Tools, which is what `xcode-select -p` reports by default.
  # Worth keeping working: not everyone building this needs a 20 GB download
  # to get an icon, and the Linux and CI paths must not care either way.
  execute_process(
    COMMAND xcrun --find actool
    OUTPUT_VARIABLE SGUMI_ACTOOL
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
    RESULT_VARIABLE SGUMI_ACTOOL_RESULT
  )

  if(NOT EXISTS ${SGUMI_ICON_PNG} AND NOT EXISTS ${SGUMI_ICON_ICON})
    message(STATUS "No icon under ${SGUMI_ICON_DIR} -- building without one.")
    set(SGUMI_ICON_NAME_KEY "")
    set(SGUMI_ICON_FILES "")

  elseif(SGUMI_ACTOOL_RESULT EQUAL 0 AND EXISTS ${SGUMI_ICON_ICON})
    message(STATUS "Icon: actool, from ${SGUMI_ICON_NAME}.icon")

    # CFBundleIconName is what sends macOS 11+ to Assets.car, where the layered
    # macOS 26 icon and every size above 128pt live. It must not be written on
    # the fallback path below, which produces no Assets.car for it to name.
    set(SGUMI_ICON_NAME_KEY
      "\n\t<key>CFBundleIconName</key>\n\t<string>${SGUMI_ICON_NAME}</string>")

    # GLOB is normally the wrong tool, but the .icon is an opaque directory
    # whose layer set changes whenever the art is re-exported, and there is no
    # manifest to read instead. CONFIGURE_DEPENDS makes CMake re-glob on build,
    # so adding a layer does not need a manual reconfigure.
    file(GLOB_RECURSE SGUMI_ICON_DEPS CONFIGURE_DEPENDS ${SGUMI_ICON_ICON}/*)

    add_custom_command(
      OUTPUT ${SGUMI_ICON_OUT}/Assets.car
             ${SGUMI_ICON_OUT}/${SGUMI_ICON_NAME}.icns
      COMMAND ${CMAKE_COMMAND} -E make_directory ${SGUMI_ICON_OUT}
      COMMAND ${SGUMI_ACTOOL} ${SGUMI_ICON_ICON}
              --compile ${SGUMI_ICON_OUT}
              --app-icon ${SGUMI_ICON_NAME}
              --output-partial-info-plist ${SGUMI_ICON_OUT}/icon.plist
              --platform macosx
              --minimum-deployment-target 13.0
              > /dev/null
      DEPENDS ${SGUMI_ICON_DEPS}
      COMMENT "Compiling ${SGUMI_ICON_NAME}.icon (actool)"
    )

    set(SGUMI_ICON_FILES
      ${SGUMI_ICON_OUT}/Assets.car
      ${SGUMI_ICON_OUT}/${SGUMI_ICON_NAME}.icns
    )

  else()
    # Names DEVELOPER_DIR because "install Xcode" is the wrong advice in the
    # common case: Xcode is already there and simply not selected, and the
    # per-invocation override is what you want over `xcode-select -s`, which
    # changes every other build on the machine and is then forgotten about.
    if(NOT SGUMI_ACTOOL_RESULT EQUAL 0)
      message(STATUS
        "Icon: no actool, falling back to ${SGUMI_ICON_NAME}.png. actool ships "
        "inside Xcode, not the Command Line Tools. If Xcode is installed but "
        "not selected, reconfigure with DEVELOPER_DIR set to its Developer "
        "directory (.../Xcode.app/Contents/Developer, or Xcode-beta.app) "
        "rather than switching your global xcode-select")
    else()
      message(STATUS
        "Icon: no ${SGUMI_ICON_NAME}.icon, falling back to "
        "${SGUMI_ICON_NAME}.png (flattened, no layered macOS 26 icon)")
    endif()

    set(SGUMI_ICON_NAME_KEY "")

    add_custom_command(
      OUTPUT ${SGUMI_ICON_OUT}/${SGUMI_ICON_NAME}.icns
      COMMAND ${CMAKE_COMMAND} -E make_directory ${SGUMI_ICON_OUT}
      COMMAND ${CMAKE_COMMAND}
              -DSRC_PNG=${SGUMI_ICON_PNG}
              -DOUT_DIR=${SGUMI_ICON_OUT}
              -DICON_NAME=${SGUMI_ICON_NAME}
              -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/make_icns.cmake
      DEPENDS ${SGUMI_ICON_PNG}
      COMMENT "Building ${SGUMI_ICON_NAME}.icns (sips/iconutil)"
    )

    set(SGUMI_ICON_FILES ${SGUMI_ICON_OUT}/${SGUMI_ICON_NAME}.icns)
  endif()

  # Finder, Spotlight and Get Info. Separate from CFBundleName below, which is
  # the menu bar -- see the template for why both exist.
  set(SGUMI_APP_DISPLAY_NAME "SGUMI")

  # @ONLY so the ${MACOSX_BUNDLE_*} placeholders survive for CMake to fill in
  # when it generates the bundle; see cmake/BundleInfo.plist.in.
  configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/cmake/BundleInfo.plist.in
    ${CMAKE_CURRENT_BINARY_DIR}/BundleInfo.plist.in
    @ONLY
  )

  set_target_properties(sgumi PROPERTIES
    MACOSX_BUNDLE TRUE
    MACOSX_BUNDLE_BUNDLE_NAME "SGUMI"
    MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
    MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
    MACOSX_BUNDLE_GUI_IDENTIFIER "com.easternedgerobotics.sgumi"
    MACOSX_BUNDLE_INFO_PLIST ${CMAKE_CURRENT_BINARY_DIR}/BundleInfo.plist.in
  )

  if(SGUMI_ICON_FILES)
    # MACOSX_PACKAGE_LOCATION works under Makefiles and Ninja, not only the
    # Xcode generator, and marking them as resources is also what keeps CMake
    # from trying to compile a .car as a source file.
    target_sources(sgumi PRIVATE ${SGUMI_ICON_FILES})

    set_source_files_properties(
      ${SGUMI_ICON_FILES}
      PROPERTIES
        GENERATED TRUE
        MACOSX_PACKAGE_LOCATION Resources
    )
  endif()

elseif(WIN32)
  # WIN32_EXECUTABLE stops a console window opening behind the app. SDL_main
  # supplies the real WinMain; see the bottom of src/main.cpp.
  set_target_properties(sgumi PROPERTIES WIN32_EXECUTABLE TRUE)

  # Explorer reads the icon out of the .exe itself, not from a file beside it,
  # so it has to be compiled in through a resource script.
  if(EXISTS ${SGUMI_ICON_ICO})
    enable_language(RC)

    set(SGUMI_RC ${CMAKE_CURRENT_BINARY_DIR}/icon.rc)

    # Generated rather than committed so the .ico path follows
    # SGUMI_ICON_NAME instead of being hardcoded in a checked-in file.
    file(WRITE ${SGUMI_RC}
      "// Generated by CMakeLists.txt -- do not edit.\n"
      "IDI_ICON1 ICON \"${SGUMI_ICON_ICO}\"\n")

    target_sources(sgumi PRIVATE ${SGUMI_RC})
  else()
    message(STATUS
      "No icon at ${SGUMI_ICON_ICO} -- building without one.")
  endif()

  # Windows looks for DLLs next to the .exe. SDL3 is the only shared target
  # CMake knows about here; libcurl comes from vcpkg, which handles its own.
  add_custom_command(TARGET sgumi POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
      $<TARGET_RUNTIME_DLLS:sgumi> $<TARGET_FILE_DIR:sgumi>
    COMMAND_EXPAND_LISTS
  )

elseif(UNIX)
  # The kiosk's own case: Alpine under xfce. A .desktop file is what puts the
  # app in the applications menu and gives the window an icon -- without it
  # xfwm falls back to a generic placeholder, and the taskbar entry is named
  # after the executable.
  #
  # StartupWMClass has to match the app ID main.cpp passes to SDL_HINT_APP_ID,
  # or the running window is not associated with this launcher and the dock
  # shows a second, unnamed entry beside it.
  set(SGUMI_DESKTOP ${CMAKE_CURRENT_BINARY_DIR}/sgumi.desktop)

  file(WRITE ${SGUMI_DESKTOP}
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Name=SGUMI\n"
    "GenericName=Label Printer\n"
    "Comment=Super Graphic Ultra Modern Interface for lipgloss\n"
    "Exec=sgumi\n"
    "Icon=sgumi\n"
    "Terminal=false\n"
    "Categories=Utility;\n"
    "StartupWMClass=com.easternedgerobotics.sgumi\n")

  install(FILES ${SGUMI_DESKTOP}
    DESTINATION ${CMAKE_INSTALL_DATAROOTDIR}/applications)

  if(EXISTS ${SGUMI_ICON_PNG})
    # hicolor/512x512 rather than a pixmaps drop: the icon theme spec is what
    # xfce actually searches, and the filename has to match Icon= above.
    install(FILES ${SGUMI_ICON_PNG}
      DESTINATION ${CMAKE_INSTALL_DATAROOTDIR}/icons/hicolor/512x512/apps
      RENAME sgumi.png)
  endif()
endif()

install(
  TARGETS sgumi
  BUNDLE  DESTINATION .
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
