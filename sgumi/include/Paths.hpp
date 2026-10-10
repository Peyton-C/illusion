#pragma once

#include <filesystem>

// Where SGUMI keeps its own files.
//
// Ported from Software_2027's apps/frontend/src/main.cpp at ff6a7f6, where the
// same three-platform logic lives inline. The only changes are the names: the
// environment variable and the two filenames. The reasoning in the comments
// below is that file's, kept because every line of it was paid for.

namespace paths {

// The config file. Read once at startup, written when the connection settings
// are saved.
//
//   macOS    ~/Library/Application Support/Eastern Edge/sgumi_config.json
//   Windows  %APPDATA%\Eastern Edge\sgumi_config.json
//   else     $XDG_CONFIG_HOME/eastern-edge/sgumi_config.json
//
// SGUMI_CONFIG_PATH overrides all three. Falls back to the working directory
// only when the OS cannot name a home -- no HOME on Unix, no Known Folder on
// Windows -- which is a genuinely broken environment rather than a normal one.
std::filesystem::path defaultConfigPath();

// Where ImGui parks window positions and sizes. Sits beside the config file.
//
// ImGui stores this pointer instead of copying the string, so the returned
// buffer has to outlive the context -- it is a function-local static, and the
// pointer is stable for the process lifetime.
const char* imguiIniPath();

}  // namespace paths
