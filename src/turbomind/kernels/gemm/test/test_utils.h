// Copyright (c) OpenMMLab. All rights reserved.

#pragma once

#include "src/turbomind/utils/macro.h"
#include <cuda_fp16.h>
#include <memory>
#include <vector>

namespace turbomind {

template<typename T>
void Compare(const T* src,
             const T* ref,
             size_t   stride,
             int      dims,
             int      bsz,
             bool     show = false,
             float    rtol = 1e-2,
             float    atol = 1e-4);

template<class T>
std::vector<float>
FastCompare(const T* src, const T* ref, int dims, int bsz, cudaStream_t stream, float rtol = 1e-2, float atol = 1e-4);

void LoadBinary(const std::string& path, size_t size, void* dst);

class RNG {
public:
    RNG();
    ~RNG();
    void GenerateUInt(uint* out, size_t count);

    template<typename T>
    void GenerateUniform(T* out, size_t count, float scale = 1.f, float shift = 0.f);

    template<typename T>
    void GenerateNormal(T* out, size_t count, float scale = 1.f, float shift = 0.f);

    cudaStream_t stream() const;

    void set_stream(cudaStream_t stream);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace turbomind
