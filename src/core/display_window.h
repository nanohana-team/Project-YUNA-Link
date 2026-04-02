#pragma once
// Project YUNA Link - display_window.h
//
// The SteamVR compositor passes a DXGI shared handle (not a raw pointer)
// via PresentInfo_t::backbufferTextureHandle.
// We open it with OpenSharedResource on our own D3D11 device, then blit.

#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <d3dcompiler.h>
#include <thread>
#include <mutex>
#include <atomic>
#include <cstdint>

class DisplayWindow
{
public:
    DisplayWindow();
    ~DisplayWindow();

    // Non-blocking: starts window + D3D thread, returns immediately.
    void Start(uint32_t eyeWidth, uint32_t eyeHeight);
    void Stop();

    // Called on SteamVR render thread.
    // handle: DXGI shared texture handle (cast to uint64_t by SteamVR)
    void Present(uint64_t sharedHandle);

    void WaitForPresent();
    bool GetTimeSinceLastVsync(float* pfSeconds, uint64_t* pulFrameCounter);

private:
    static LRESULT CALLBACK WndProc(HWND, UINT, WPARAM, LPARAM);
    void   WindowThread();
    bool   InitD3D(HWND hWnd);
    void   BlitShared(HANDLE hShared);
    void   Cleanup();

    template<class T> static void SR(T*& p)
    { if (p) { p->Release(); p = nullptr; } }

    uint32_t m_eyeW = 3840;
    uint32_t m_eyeH = 3840;

    std::thread       m_thread;
    std::atomic<bool> m_running{ false };
    std::atomic<bool> m_d3dReady{ false };

    // Pending shared handle, signaled each Present()
    std::mutex   m_handleMtx;
    HANDLE       m_pendingHandle = nullptr;
    HANDLE       m_frameReady    = INVALID_HANDLE_VALUE;

    HWND                    m_hWnd    = nullptr;
    ID3D11Device*           m_dev     = nullptr;
    ID3D11DeviceContext*    m_ctx     = nullptr;
    IDXGISwapChain*         m_swap    = nullptr;
    ID3D11RenderTargetView* m_rtv     = nullptr;
    ID3D11VertexShader*     m_vs      = nullptr;
    ID3D11PixelShader*      m_ps      = nullptr;
    ID3D11SamplerState*     m_sampler = nullptr;
    ID3D11Buffer*           m_vb      = nullptr;
    ID3D11InputLayout*      m_layout  = nullptr;

    mutable std::mutex m_vsyncMtx;
    LARGE_INTEGER      m_lastPresent{};
    uint64_t           m_frameCount = 0;
    LARGE_INTEGER      m_freq{};
};
