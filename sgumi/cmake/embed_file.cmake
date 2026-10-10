# Any file -> a C array in a header, so the bytes travel inside the executable.
#
# Run as a script (cmake -P) from a custom command, with:
#   INPUT   the file to embed
#   OUTPUT  the header to write
#   SYMBOL  the array's name
#
# Used for the Linux window icon, which has to be in the binary rather than
# read from the install prefix: the app cannot know where it was installed,
# and it should have an icon when run straight out of the build directory too.
#
# Meant for small files. Every byte becomes five characters of source, so a
# megabyte of input is five megabytes for the compiler to chew through.

file(READ ${INPUT} HEX_BYTES HEX)
string(REGEX REPLACE "([0-9a-f][0-9a-f])" "0x\\1," ARRAY_BODY "${HEX_BYTES}")

file(WRITE ${OUTPUT}
  "#pragma once\n"
  "// Generated at build time by cmake/embed_file.cmake -- do not edit.\n"
  "inline constexpr unsigned char ${SYMBOL}[] = {${ARRAY_BODY}};\n"
)
