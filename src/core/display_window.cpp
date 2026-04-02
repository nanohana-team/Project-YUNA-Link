// Project YUNA Link - display_window.cpp
// Libs: d3d11.lib dxgi.lib d3dcompiler.lib  (linked via vcxproj)
//
// HMD driver remains full stereo (both eyes).
// This window shows RIGHT EYE ONLY for capture/YOLO use.
// The compositor delivers a side-by-side texture (3840x1920).
// We sample only the right half (U: 0.5~1.0).

#include "driver_main.h"
#include "display_window.h"
#include <cstring>
#include <cstdio>
#include <dxgi1_2.h>  // IDXGIKeyedMutex

// ---------------------------------------------------------------------------
// HLSL: simple full-texture blit (UV crop is done via CopySubresourceRegion)
// ---------------------------------------------------------------------------
static const char kVS[] = R"(
struct V { float2 p:POSITION; float2 t:TEXCOORD; };
struct P { float4 p:SV_POSITION; float2 t:TEXCOORD; };
P main(V v){ P o; o.p=float4(v.p,0,1); o.t=v.t; return o; }
)";
static const char kPS[] = R"(
Texture2D tx:register(t0); SamplerState s:register(s0);
struct P { float4 p:SV_POSITION; float2 t:TEXCOORD; };
float4 main(P v):SV_TARGET{ return tx.Sample(s,v.t); }
)";

struct Vtx { float x,y,u,v; };
static const Vtx kQuad[6] = {
    {-1.f, 1.f, 0.f, 0.f}, { 1.f, 1.f, 1.f, 0.f}, { 1.f,-1.f, 1.f, 1.f},
    {-1.f, 1.f, 0.f, 0.f}, { 1.f,-1.f, 1.f, 1.f}, {-1.f,-1.f, 0.f, 1.f},
};

// ---------------------------------------------------------------------------
DisplayWindow::DisplayWindow()
{
    QueryPerformanceFrequency(&m_freq);
    m_frameReady = CreateEventA(nullptr, FALSE, FALSE, nullptr);
}

DisplayWindow::~DisplayWindow() { Stop(); }

// ---------------------------------------------------------------------------
void DisplayWindow::Start(uint32_t eyeW, uint32_t eyeH)
{
    m_eyeW = eyeW; m_eyeH = eyeH;
    m_running = true;
    m_thread  = std::thread(&DisplayWindow::WindowThread, this);
}

void DisplayWindow::Stop()
{
    if (!m_running.exchange(false)) return;
    if (m_hWnd) PostMessageA(m_hWnd, WM_QUIT, 0, 0);
    if (m_thread.joinable()) m_thread.join();
    Cleanup();
    if (m_frameReady != INVALID_HANDLE_VALUE)
    { CloseHandle(m_frameReady); m_frameReady = INVALID_HANDLE_VALUE; }
}

// ---------------------------------------------------------------------------
LRESULT CALLBACK DisplayWindow::WndProc(HWND hWnd, UINT msg,
                                          WPARAM wp, LPARAM lp)
{
    if (msg == WM_DESTROY){ PostQuitMessage(0); return 0; }
    return DefWindowProcA(hWnd, msg, wp, lp);
}

void DisplayWindow::WindowThread()
{
    char cls[64];
    snprintf(cls, sizeof(cls), "YunaDisp_%p", (void*)this);

    WNDCLASSEXA wc{};
    wc.cbSize=sizeof(wc); wc.style=CS_HREDRAW|CS_VREDRAW;
    wc.lpfnWndProc=WndProc; wc.hInstance=GetModuleHandleA(nullptr);
    wc.hCursor=LoadCursor(nullptr,IDC_ARROW); wc.lpszClassName=cls;
    if (!RegisterClassExA(&wc))
    {
        DriverLog("[YUNA Display] RegisterClass failed (%lu)\n", GetLastError());
        return;
    }

    RECT rc{ 0,0,720,720 };
    AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);

    m_hWnd = CreateWindowExA(0, cls, "YUNA Link - VR View",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        CW_USEDEFAULT, CW_USEDEFAULT,
        rc.right - rc.left, rc.bottom - rc.top,
        nullptr, nullptr, GetModuleHandleA(nullptr), nullptr);

    if (!m_hWnd || !InitD3D(m_hWnd))
    {
        DriverLog("[YUNA Display] Window/D3D init failed\n");
        if (m_hWnd){ DestroyWindow(m_hWnd); m_hWnd=nullptr; }
        UnregisterClassA(cls, GetModuleHandleA(nullptr));
        return;
    }

    // Bring window to foreground
    SetForegroundWindow(m_hWnd);
    ShowWindow(m_hWnd, SW_SHOW);

    m_d3dReady = true;
    DriverLog("[YUNA Display] Window ready %ux%u (right eye only)\n", m_eyeW, m_eyeH);

    MSG msg{};
    while (m_running)
    {
        while (PeekMessageA(&msg,nullptr,0,0,PM_REMOVE))
        {
            if (msg.message==WM_QUIT){ m_running=false; break; }
            TranslateMessage(&msg); DispatchMessageA(&msg);
        }
        if (!m_running) break;

        // Wait for a new shared handle from Present()
        if (WaitForSingleObject(m_frameReady, 33) == WAIT_OBJECT_0)
        {
            HANDLE h = nullptr;
            {
                std::lock_guard<std::mutex> lk(m_handleMtx);
                h = m_pendingHandle;
                m_pendingHandle = nullptr;
            }
            if (h) BlitShared(h);
        }
    }

    Cleanup();
    UnregisterClassA(cls, GetModuleHandleA(nullptr));
}

// ---------------------------------------------------------------------------
bool DisplayWindow::InitD3D(HWND hWnd)
{
    DXGI_SWAP_CHAIN_DESC sd{};
    sd.BufferCount=2;
    sd.BufferDesc.Width=m_eyeW; sd.BufferDesc.Height=m_eyeH;  // right eye only
    sd.BufferDesc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.BufferDesc.RefreshRate={90,1};
    sd.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow=hWnd; sd.SampleDesc.Count=1;
    sd.Windowed=TRUE; sd.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;

    // Must create with D3D11_CREATE_DEVICE_BGRA_SUPPORT for shared textures
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
        &fl,1,D3D11_SDK_VERSION, &sd,&m_swap,&m_dev,nullptr,&m_ctx);
    if (FAILED(hr))
    { DriverLog("[YUNA Display] D3D11 create failed 0x%08X\n",(unsigned)hr); return false; }

    ID3D11Texture2D* bb=nullptr;
    m_swap->GetBuffer(0,__uuidof(ID3D11Texture2D),(void**)&bb);
    m_dev->CreateRenderTargetView(bb,nullptr,&m_rtv); bb->Release();

    // Compile shaders
    ID3DBlob *vsB=nullptr,*psB=nullptr,*err=nullptr;
    if (FAILED(D3DCompile(kVS,strlen(kVS),nullptr,nullptr,nullptr,
                           "main","vs_5_0",0,0,&vsB,&err)))
    { DriverLog("[YUNA Display] VS: %s\n",
        err?(char*)err->GetBufferPointer():"?");
      if(err)err->Release(); return false; }
    if (FAILED(D3DCompile(kPS,strlen(kPS),nullptr,nullptr,nullptr,
                           "main","ps_5_0",0,0,&psB,&err)))
    { DriverLog("[YUNA Display] PS: %s\n",
        err?(char*)err->GetBufferPointer():"?");
      if(err)err->Release(); vsB->Release(); return false; }

    m_dev->CreateVertexShader(vsB->GetBufferPointer(),vsB->GetBufferSize(),nullptr,&m_vs);
    m_dev->CreatePixelShader (psB->GetBufferPointer(),psB->GetBufferSize(),nullptr,&m_ps);

    D3D11_INPUT_ELEMENT_DESC ied[]={
        {"POSITION",0,DXGI_FORMAT_R32G32_FLOAT,0, 0,D3D11_INPUT_PER_VERTEX_DATA,0},
        {"TEXCOORD",0,DXGI_FORMAT_R32G32_FLOAT,0, 8,D3D11_INPUT_PER_VERTEX_DATA,0},
    };
    m_dev->CreateInputLayout(ied,2,vsB->GetBufferPointer(),vsB->GetBufferSize(),&m_layout);
    vsB->Release(); psB->Release();

    D3D11_BUFFER_DESC bd{}; bd.ByteWidth=sizeof(kQuad);
    bd.Usage=D3D11_USAGE_IMMUTABLE; bd.BindFlags=D3D11_BIND_VERTEX_BUFFER;
    D3D11_SUBRESOURCE_DATA idata{kQuad};
    m_dev->CreateBuffer(&bd,&idata,&m_vb);


    D3D11_SAMPLER_DESC smpd{};
    smpd.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    smpd.AddressU=smpd.AddressV=smpd.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;
    m_dev->CreateSamplerState(&smpd,&m_sampler);

    D3D11_VIEWPORT vp{};
    vp.Width=(float)(m_eyeW); vp.Height=(float)m_eyeH; vp.MaxDepth=1.f;  // right eye only
    m_ctx->RSSetViewports(1,&vp);

    return true;
}

// ---------------------------------------------------------------------------
// Present: stash shared handle, signal window thread (called on SteamVR thread)
// ---------------------------------------------------------------------------
void DisplayWindow::Present(uint64_t sharedHandle)
{
    if (!m_d3dReady || !sharedHandle) return;

    {
        std::lock_guard<std::mutex> lk(m_handleMtx);
        m_pendingHandle = reinterpret_cast<HANDLE>(sharedHandle);
    }
    SetEvent(m_frameReady);

    {
        std::lock_guard<std::mutex> lk(m_vsyncMtx);
        QueryPerformanceCounter(&m_lastPresent);
        ++m_frameCount;
    }
}

// ---------------------------------------------------------------------------
// BlitShared: open shared texture on our device and blit (window thread)
// ---------------------------------------------------------------------------
void DisplayWindow::BlitShared(HANDLE hShared)
{
    // Open the compositor's texture on our device via the DXGI shared handle
    ID3D11Texture2D* sharedTex = nullptr;
    HRESULT hr = m_dev->OpenSharedResource(
        hShared, __uuidof(ID3D11Texture2D), (void**)&sharedTex);
    if (FAILED(hr))
    {
        static bool s_logged = false;
        if (!s_logged)
        {
            DriverLog("[YUNA Display] OpenSharedResource failed 0x%08X"
                      " handle=%p\n", (unsigned)hr, hShared);
            s_logged = true;
        }
        return;
    }

    // Log texture size (first time only, but stores it for UV decision)
    static uint32_t s_texW = 0, s_texH = 0;
    {
        D3D11_TEXTURE2D_DESC tmp{}; sharedTex->GetDesc(&tmp);
        if (s_texW != tmp.Width || s_texH != tmp.Height)
        {
            s_texW = tmp.Width; s_texH = tmp.Height;
            DriverLog("[YUNA Display] Texture size changed: %ux%u fmt=%u eyeW=%u\n",
                      s_texW, s_texH, (unsigned)tmp.Format, m_eyeW);
            DriverLog("[YUNA Display] -> %s\n",
                      s_texW >= m_eyeW*2 ? "side-by-side: will crop right half"
                                         : "single eye: full blit");
        }
    }

    // Acquire IDXGIKeyedMutex if the texture has one (cross-device sync)
    IDXGIKeyedMutex* km = nullptr;
    sharedTex->QueryInterface(__uuidof(IDXGIKeyedMutex), (void**)&km);
    if (km)
    {
        hr = km->AcquireSync(0, 100); // wait up to 100ms
        if (FAILED(hr))
        {
            km->Release();
            sharedTex->Release();
            return;
        }
    }

    // Build SRV on the shared texture
    D3D11_TEXTURE2D_DESC desc{};
    sharedTex->GetDesc(&desc);

    // Determine crop region: if side-by-side (width >= 2*eyeW), use right half
    bool isSideBySide = (desc.Width >= m_eyeW * 2);
    uint32_t srcX     = 0;
    uint32_t srcW     = m_eyeW;
    uint32_t srcH     = m_eyeH;

    // Create a staging texture of single-eye size to receive the crop
    D3D11_TEXTURE2D_DESC stageDesc{};
    stageDesc.Width            = srcW;
    stageDesc.Height           = srcH;
    stageDesc.MipLevels        = 1;
    stageDesc.ArraySize        = 1;
    stageDesc.Format           = (desc.Format == DXGI_FORMAT_R8G8B8A8_TYPELESS ||
                                   desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM)
                                  ? DXGI_FORMAT_R8G8B8A8_UNORM
                                  : DXGI_FORMAT_B8G8R8A8_UNORM;
    stageDesc.SampleDesc.Count = 1;
    stageDesc.Usage            = D3D11_USAGE_DEFAULT;
    stageDesc.BindFlags        = D3D11_BIND_SHADER_RESOURCE;

    ID3D11Texture2D* cropTex = nullptr;
    HRESULT hr2 = m_dev->CreateTexture2D(&stageDesc, nullptr, &cropTex);

    if (SUCCEEDED(hr2))
    {
        // Copy right half of shared texture into cropTex
        D3D11_BOX box{};
        box.left   = srcX;
        box.right  = srcX + srcW;
        box.top    = 0;
        box.bottom = srcH;
        box.front  = 0;
        box.back   = 1;
        m_ctx->CopySubresourceRegion(cropTex, 0, 0, 0, 0, sharedTex, 0, &box);
    }

    // Release KeyedMutex after copy (before we use GPU)
    if (km)
    {
        km->ReleaseSync(0);
        km->Release();
    }
    sharedTex->Release();

    if (FAILED(hr2) || !cropTex)
    {
        DriverLog("[YUNA Display] CreateTexture2D(crop) failed 0x%08X\n",(unsigned)hr2);
        return;
    }

    // Build SRV on the cropped single-eye texture
    D3D11_SHADER_RESOURCE_VIEW_DESC srvd{};
    srvd.Format                    = stageDesc.Format;
    srvd.ViewDimension             = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvd.Texture2D.MipLevels       = 1;

    ID3D11ShaderResourceView* srv = nullptr;
    hr = m_dev->CreateShaderResourceView(cropTex, &srvd, &srv);
    cropTex->Release();

    if (FAILED(hr))
    {
        DriverLog("[YUNA Display] CreateSRV failed 0x%08X\n",(unsigned)hr);
        return;
    }

    // Viewport: single eye size (window is EYE_W x EYE_H)
    D3D11_VIEWPORT vp{};
    vp.Width    = (float)m_eyeW;
    vp.Height   = (float)m_eyeH;
    vp.MaxDepth = 1.f;
    m_ctx->RSSetViewports(1, &vp);

    // Draw full-screen quad (PS samples right half of stereo texture)
    float clr[4]={0,0,0,1};
    m_ctx->ClearRenderTargetView(m_rtv,clr);
    m_ctx->OMSetRenderTargets(1,&m_rtv,nullptr);
    m_ctx->IASetInputLayout(m_layout);
    UINT stride=sizeof(Vtx),off=0;
    m_ctx->IASetVertexBuffers(0,1,&m_vb,&stride,&off);
    m_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    m_ctx->VSSetShader(m_vs,nullptr,0);
    m_ctx->PSSetShader(m_ps,nullptr,0);
    m_ctx->PSSetShaderResources(0,1,&srv);
    m_ctx->PSSetSamplers(0,1,&m_sampler);
    m_ctx->Draw(6,0);

    // Unbind SRV before releasing
    ID3D11ShaderResourceView* nullSrv = nullptr;
    m_ctx->PSSetShaderResources(0,1,&nullSrv);
    srv->Release();

    m_swap->Present(0,0);
}

// ---------------------------------------------------------------------------
void DisplayWindow::WaitForPresent()
{
    if (m_frameReady != INVALID_HANDLE_VALUE)
        WaitForSingleObject(m_frameReady, 100);
}

bool DisplayWindow::GetTimeSinceLastVsync(float* pfSec, uint64_t* pFrame)
{
    std::lock_guard<std::mutex> lk(m_vsyncMtx);
    if (!m_frameCount) return false;
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    *pfSec  = (float)(now.QuadPart-m_lastPresent.QuadPart)/(float)m_freq.QuadPart;
    *pFrame = m_frameCount;
    return true;
}

void DisplayWindow::Cleanup()
{
    SR(m_layout); SR(m_vb); SR(m_sampler);
    SR(m_ps); SR(m_vs); SR(m_rtv); SR(m_swap);
    if (m_ctx){ m_ctx->ClearState(); SR(m_ctx); }
    SR(m_dev);
    m_hWnd = nullptr;
}
