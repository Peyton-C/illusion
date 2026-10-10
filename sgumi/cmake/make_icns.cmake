# PNG -> .icns, for macOS builds on a machine without Xcode.
#
# Run as a script (cmake -P) from a custom command, with:
#   SRC_PNG    the 1024x1024 source
#   OUT_DIR    where AppIcon.icns should land
#   ICON_NAME  basename for the .icns, matching CFBundleIconFile
#
# sips and iconutil are both /usr/bin -- part of macOS itself, not of the
# Command Line Tools and not of Xcode -- so this path works on any Mac. What it
# cannot do is the layered macOS 26 icon: that needs actool compiling the
# .icon into an Assets.car, and actool ships only inside Xcode. See the
# frontend's CMakeLists for which branch runs when.
#
# The ten representations below are the full set an .icns can hold. actool
# emits only four, because it expects Assets.car to carry everything above
# 128pt; with no Assets.car to fall back on, this file has to cover the range
# itself or the Dock gets a blurry upscale.

set(ICONSET ${OUT_DIR}/${ICON_NAME}.iconset)

# From scratch every time. iconutil packs whatever it finds in the directory,
# so a stale representation left over from an earlier icon would be silently
# baked into the new one.
file(REMOVE_RECURSE ${ICONSET})
file(MAKE_DIRECTORY ${ICONSET})

foreach(SIZE 16 32 128 256 512)
  math(EXPR RETINA "${SIZE} * 2")

  execute_process(
    COMMAND sips -z ${SIZE} ${SIZE} ${SRC_PNG}
            --out ${ICONSET}/icon_${SIZE}x${SIZE}.png
    RESULT_VARIABLE RESULT
    OUTPUT_QUIET
    ERROR_QUIET
  )

  if(NOT RESULT EQUAL 0)
    message(FATAL_ERROR "sips failed on ${SRC_PNG} at ${SIZE}x${SIZE}")
  endif()

  execute_process(
    COMMAND sips -z ${RETINA} ${RETINA} ${SRC_PNG}
            --out ${ICONSET}/icon_${SIZE}x${SIZE}@2x.png
    RESULT_VARIABLE RESULT
    OUTPUT_QUIET
    ERROR_QUIET
  )

  if(NOT RESULT EQUAL 0)
    message(FATAL_ERROR "sips failed on ${SRC_PNG} at ${RETINA}x${RETINA}")
  endif()
endforeach()

execute_process(
  COMMAND iconutil --convert icns ${ICONSET} --output ${OUT_DIR}/${ICON_NAME}.icns
  RESULT_VARIABLE RESULT
)

if(NOT RESULT EQUAL 0)
  message(FATAL_ERROR "iconutil failed to pack ${ICONSET}")
endif()
