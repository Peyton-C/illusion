# Stamps the current date into build_info.h. Run on every build (see the
# custom target in ../CMakeLists.txt) so the date can never go stale.
#
# The write goes through a temp file and configure_file(COPYONLY), which only
# touches the destination when the contents actually differ. That matters: the
# header is included by main.cpp, so rewriting it unconditionally would force a
# recompile and relink on every single build. At date granularity the file
# changes at most once a day, and rebuilds within the same day are free.

string(TIMESTAMP EER_BUILD_DATE "%Y-%m-%d")

file(WRITE ${OUTPUT}.tmp
  "#pragma once\n"
  "// Generated at build time by cmake/write_build_info.cmake -- do not edit.\n"
  "#define EER_BUILD_DATE \"${EER_BUILD_DATE}\"\n"
)

configure_file(${OUTPUT}.tmp ${OUTPUT} COPYONLY)
file(REMOVE ${OUTPUT}.tmp)
