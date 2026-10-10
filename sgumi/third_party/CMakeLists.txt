# Vendored upstream code. Nothing in the subdirectories here is edited -- they
# are updated by bumping the checkout. The wrappers below exist only for
# projects that ship no build system of their own, and live at this level so
# the upstream trees stay pristine.
#
# Ported from Software_2027's third_party/CMakeLists.txt at ff6a7f6, minus the
# RtMidi block (no control surface here) and minus the two Python patch scripts
# (both fix iOS-only bugs in code paths SGUMI does not reach -- the SDL one is
# a Simulator GPU workaround, the ImGui one a Metal shader blob only the video
# path trips over). The four checkouts are pinned to the same commits that repo
# uses, so a bug reproduced there reproduces here.

add_subdirectory(sdl EXCLUDE_FROM_ALL)

# SDL3 exports a different target name depending on how it was configured, so
# everything downstream links this alias instead of guessing.
add_library(sgumi_sdl3 INTERFACE)
add_library(sgumi::sdl3 ALIAS sgumi_sdl3)

if(TARGET SDL3::SDL3)
  target_link_libraries(sgumi_sdl3 INTERFACE SDL3::SDL3)
elseif(TARGET SDL3::SDL3-shared)
  target_link_libraries(sgumi_sdl3 INTERFACE SDL3::SDL3-shared)
elseif(TARGET SDL3::SDL3-static)
  target_link_libraries(sgumi_sdl3 INTERFACE SDL3::SDL3-static)
else()
  message(FATAL_ERROR
    "SDL3 is vendored at third_party/sdl, but no usable SDL3 target exists. "
    "Is the checkout empty? git submodule update --init --recursive"
  )
endif()

add_subdirectory(json EXCLUDE_FROM_ALL)

# Dear ImGui ships sources but no CMake, so we compile the core plus the two
# backends this project uses.
add_library(sgumi_imgui STATIC
  imgui/imgui.cpp
  imgui/imgui_draw.cpp
  imgui/imgui_widgets.cpp
  imgui/imgui_tables.cpp
  imgui/backends/imgui_impl_sdl3.cpp
  imgui/backends/imgui_impl_sdlgpu3.cpp
)

add_library(sgumi::imgui ALIAS sgumi_imgui)

# Both roots are exposed: sources include "imgui.h" and, for the backends,
# "backends/imgui_impl_sdl3.h".
target_include_directories(sgumi_imgui
  PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/imgui
    ${CMAKE_CURRENT_SOURCE_DIR}/imgui/backends
)

target_link_libraries(sgumi_imgui PUBLIC sgumi::sdl3)

# stb is header-only. Nothing links it yet; it is here for the label preview,
# which decodes the PNG /preview returns and uploads it as a texture. Costs
# nothing until something includes stb_image.h.
add_library(sgumi_stb INTERFACE)
add_library(sgumi::stb ALIAS sgumi_stb)

target_include_directories(sgumi_stb
  INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/stb
)
