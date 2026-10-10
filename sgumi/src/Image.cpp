#include "Image.hpp"

// stb_image is a single-header library: exactly one translation unit in the
// program may define this before including it. Keeping that definition in a
// .cpp rather than the header is what stops a second includer emitting a
// duplicate copy of the implementation.
#define STB_IMAGE_IMPLEMENTATION
#include <stb_image.h>

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb_image_write.h>

#define STB_IMAGE_RESIZE_IMPLEMENTATION
#include <stb_image_resize2.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <iostream>

namespace image {

Texture::~Texture() {
    reset();
}

void Texture::reset() {
    if (!texture_) {
        return;
    }

    // Documented as freeing the texture once the GPU is finished with it, so
    // this is safe even for a texture the frame in flight is still sampling.
    SDL_ReleaseGPUTexture(device_, texture_);

    texture_ = nullptr;
    width_ = 0;
    height_ = 0;
}

bool Texture::load(SDL_GPUDevice* device, const void* data, size_t length) {
    int width = 0;
    int height = 0;
    int channels = 0;

    // Forced to 4 channels: lipgloss sends RGB, the GPU format below is RGBA,
    // and letting stb convert is cheaper than a second format to handle.
    unsigned char* pixels = stbi_load_from_memory(
        static_cast<const unsigned char*>(data),
        static_cast<int>(length),
        &width,
        &height,
        &channels,
        4);

    if (!pixels) {
        std::cerr << "Preview: could not decode PNG: " << stbi_failure_reason()
                  << std::endl;
        return false;
    }

    SDL_GPUTextureCreateInfo texInfo = {};
    texInfo.type = SDL_GPU_TEXTURETYPE_2D;
    texInfo.format = SDL_GPU_TEXTUREFORMAT_R8G8B8A8_UNORM;
    texInfo.usage = SDL_GPU_TEXTUREUSAGE_SAMPLER;
    texInfo.width = static_cast<Uint32>(width);
    texInfo.height = static_cast<Uint32>(height);
    texInfo.layer_count_or_depth = 1;
    texInfo.num_levels = 1;

    SDL_GPUTexture* texture = SDL_CreateGPUTexture(device, &texInfo);

    if (!texture) {
        std::cerr << "Preview: could not create texture: " << SDL_GetError()
                  << std::endl;
        stbi_image_free(pixels);
        return false;
    }

    const size_t byteCount = static_cast<size_t>(width) * height * 4;

    SDL_GPUTransferBufferCreateInfo tbInfo = {};
    tbInfo.usage = SDL_GPU_TRANSFERBUFFERUSAGE_UPLOAD;
    tbInfo.size = static_cast<Uint32>(byteCount);

    SDL_GPUTransferBuffer* transfer = SDL_CreateGPUTransferBuffer(device, &tbInfo);

    if (!transfer) {
        std::cerr << "Preview: could not create transfer buffer: "
                  << SDL_GetError() << std::endl;
        SDL_ReleaseGPUTexture(device, texture);
        stbi_image_free(pixels);
        return false;
    }

    void* mapped = SDL_MapGPUTransferBuffer(device, transfer, false);

    if (!mapped) {
        std::cerr << "Preview: could not map transfer buffer: "
                  << SDL_GetError() << std::endl;
        SDL_ReleaseGPUTransferBuffer(device, transfer);
        SDL_ReleaseGPUTexture(device, texture);
        stbi_image_free(pixels);
        return false;
    }

    std::memcpy(mapped, pixels, byteCount);
    SDL_UnmapGPUTransferBuffer(device, transfer);
    stbi_image_free(pixels);

    SDL_GPUCommandBuffer* cmd = SDL_AcquireGPUCommandBuffer(device);
    SDL_GPUCopyPass* copyPass = SDL_BeginGPUCopyPass(cmd);

    SDL_GPUTextureTransferInfo src = {};
    src.transfer_buffer = transfer;

    SDL_GPUTextureRegion dst = {};
    dst.texture = texture;
    dst.w = static_cast<Uint32>(width);
    dst.h = static_cast<Uint32>(height);
    dst.d = 1;

    SDL_UploadToGPUTexture(copyPass, &src, &dst, false);
    SDL_EndGPUCopyPass(copyPass);
    SDL_SubmitGPUCommandBuffer(cmd);
    SDL_ReleaseGPUTransferBuffer(device, transfer);

    // Only now that the new one is definitely good: a failed decode above
    // leaves the previous preview on screen rather than blanking it.
    reset();

    device_ = device;
    texture_ = texture;
    width_ = width;
    height_ = height;

    return true;
}

bool decode(const void* data, size_t length, Bitmap& out) {
    int width = 0;
    int height = 0;
    int channels = 0;

    // Four channels for the same reason Texture::load asks for them: everything
    // downstream of here -- the rotate, the resample, the PNG writer and the
    // upload to the GPU -- is simpler with one pixel layout than with six.
    unsigned char* pixels = stbi_load_from_memory(
        static_cast<const unsigned char*>(data),
        static_cast<int>(length),
        &width, &height, &channels, 4);

    if (!pixels) {
        return false;
    }

    out.width = width;
    out.height = height;
    out.pixels.assign(pixels, pixels + static_cast<size_t>(width) * height * 4);

    stbi_image_free(pixels);
    return true;
}

bool decodeFile(const char* path, Bitmap& out, std::string& error) {
    std::FILE* file = std::fopen(path, "rb");

    if (!file) {
        error = "Could not open that file.";
        return false;
    }

    std::string contents;
    char chunk[16384];

    while (const size_t read = std::fread(chunk, 1, sizeof(chunk), file)) {
        contents.append(chunk, read);
    }

    std::fclose(file);

    if (contents.empty()) {
        error = "That file is empty.";
        return false;
    }

    if (!decode(contents.data(), contents.size(), out)) {
        // stb's reason is terse but specific, and beats "decoding failed" when
        // someone has picked a HEIC out of a photo library.
        const char* reason = stbi_failure_reason();
        error = std::string("Could not read that as an image") +
                (reason ? std::string(": ") + reason : "") + ".";
        return false;
    }

    return true;
}

Bitmap rotated(const Bitmap& source, int quarterTurns) {
    const int turns = ((quarterTurns % 4) + 4) % 4;

    if (!source.valid() || turns == 0) {
        return source;
    }

    Bitmap out;
    const bool swaps = turns % 2 == 1;

    out.width = swaps ? source.height : source.width;
    out.height = swaps ? source.width : source.height;
    out.pixels.resize(static_cast<size_t>(out.width) * out.height * 4);

    for (int y = 0; y < source.height; ++y) {
        for (int x = 0; x < source.width; ++x) {
            int nx = 0;
            int ny = 0;

            // Clockwise: the top-left pixel ends up top-right after one turn.
            switch (turns) {
            case 1:
                nx = source.height - 1 - y;
                ny = x;
                break;
            case 2:
                nx = source.width - 1 - x;
                ny = source.height - 1 - y;
                break;
            default:
                nx = y;
                ny = source.width - 1 - x;
                break;
            }

            const size_t from = (static_cast<size_t>(y) * source.width + x) * 4;
            const size_t to = (static_cast<size_t>(ny) * out.width + nx) * 4;

            std::memcpy(&out.pixels[to], &source.pixels[from], 4);
        }
    }

    return out;
}

Bitmap scaled(const Bitmap& source, int width, int height) {
    if (!source.valid()) {
        return source;
    }

    Bitmap out;
    out.width = std::max(width, 1);
    out.height = std::max(height, 1);
    out.pixels.resize(static_cast<size_t>(out.width) * out.height * 4);

    // Straight sRGB rather than premultiplied: nothing here composites, and a
    // label is opaque by the time the printer sees it.
    stbir_resize_uint8_srgb(
        source.pixels.data(), source.width, source.height, 0,
        out.pixels.data(), out.width, out.height, 0,
        STBIR_RGBA);

    return out;
}

Bitmap paddedTo(const Bitmap& source, int width, int height) {
    if (!source.valid() ||
        (width <= source.width && height <= source.height)) {
        return source;
    }

    Bitmap out;
    out.width = std::max(width, source.width);
    out.height = std::max(height, source.height);

    // Opaque white, because this is label stock: 255 across all four channels
    // rather than a transparent field, which lipgloss would only have to
    // composite onto white anyway.
    out.pixels.assign(static_cast<size_t>(out.width) * out.height * 4, 255);

    // Left over pixels go after the image rather than before it, so an odd
    // margin leans one pixel toward the far edge rather than splitting a pixel
    // that cannot be split.
    const int offsetX = (out.width - source.width) / 2;
    const int offsetY = (out.height - source.height) / 2;

    for (int y = 0; y < source.height; ++y) {
        const size_t from = static_cast<size_t>(y) * source.width * 4;
        const size_t to =
            (static_cast<size_t>(y + offsetY) * out.width + offsetX) * 4;

        std::memcpy(&out.pixels[to], &source.pixels[from],
                    static_cast<size_t>(source.width) * 4);
    }

    return out;
}

bool encodePng(const Bitmap& source, std::string& out) {
    if (!source.valid()) {
        return false;
    }

    out.clear();

    // Written through a callback rather than to a file: this goes straight into
    // a multipart body, and a temporary on disk would be one more thing to
    // clean up on a kiosk that gets power-cycled.
    const auto append = [](void* context, void* data, int size) {
        static_cast<std::string*>(context)->append(
            static_cast<const char*>(data), static_cast<size_t>(size));
    };

    const int ok = stbi_write_png_to_func(
        append, &out, source.width, source.height, 4,
        source.pixels.data(), source.width * 4);

    return ok != 0 && !out.empty();
}

}  // namespace image
