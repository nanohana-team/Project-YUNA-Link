// Project YUNA Link - display_window.cpp
// Libs: d3d11.lib dxgi.lib d3dcompiler.lib  (linked via vcxproj)

#include "driver_main.h"
#include "display_window.h"
#include <cstring>
#include <cstdio>

// ---------------------------------------------------------------------------
// HLSL: full-screen blit quad
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

    RECT rc{0,0,(LONG)(m_eyeW*2),(LONG)m_eyeH};
    AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
    m_hWnd = CreateWindowExA(0, cls, "YUNA Link - VR View",
        WS_OVERLAPPEDWINDOW|WS_VISIBLE,
        CW_USEDEFAULT, CW_USEDEFAULT,
        rc.right-rc.left, rc.bottom-rc.top,
        nullptr,nullptr,GetModuleHandleA(nullptr),nullptr);

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
    DriverLog("[YUNA Display] Window ready %ux%u per eye\n", m_eyeW, m_eyeH);

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
    sd.BufferDesc.Width=m_eyeW*2; sd.BufferDesc.Height=m_eyeH;
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
    vp.Width=(float)(m_eyeW*2); vp.Height=(float)m_eyeH; vp.MaxDepth=1.f;
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
    ID3D11Texture2D* sharedTex = nullptr;
    HRESULT hr = m_dev->OpenSharedResource(
        hShared, __uuidof(ID3D11Texture2D), (void**)&sharedTex);
    if (FAILED(hr) || !sharedTex) return;

    IDXGIKeyedMutex* keyedMutex = nullptr;
    hr = sharedTex->QueryInterface(__uuidof(IDXGIKeyedMutex), (void**)&keyedMutex);
    if (SUCCEEDED(hr) && keyedMutex)
    {
        hr = keyedMutex->AcquireSync(0, 10);
        if (hr != S_OK)
        {
            keyedMutex->Release();
            sharedTex->Release();
            return;
        }
    }

    D3D11_TEXTURE2D_DESC desc{};
    sharedTex->GetDesc(&desc);

    D3D11_SHADER_RESOURCE_VIEW_DESC srvd{};
    switch (desc.Format)
    {
    case DXGI_FORMAT_R8G8B8A8_TYPELESS:
        srvd.Format = DXGI_FORMAT_R8G8B8A8_UNORM; break;
    case DXGI_FORMAT_B8G8R8A8_TYPELESS:
        srvd.Format = DXGI_FORMAT_B8G8R8A8_UNORM; break;
    default:
        srvd.Format = desc.Format; break;
    }
    srvd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvd.Texture2D.MipLevels = 1;

    ID3D11ShaderResourceView* srv = nullptr;
    hr = m_dev->CreateShaderResourceView(sharedTex, &srvd, &srv);
    if (SUCCEEDED(hr) && srv)
    {
        float clr[4] = {0,0,0,1};
        m_ctx->ClearRenderTargetView(m_rtv, clr);
        m_ctx->OMSetRenderTargets(1, &m_rtv, nullptr);

        UINT stride = sizeof(Vtx), off = 0;
        m_ctx->IASetInputLayout(m_layout);
        m_ctx->IASetVertexBuffers(0, 1, &m_vb, &stride, &off);
        m_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        m_ctx->VSSetShader(m_vs, nullptr, 0);
        m_ctx->PSSetShader(m_ps, nullptr, 0);
        m_ctx->PSSetShaderResources(0, 1, &srv);
        m_ctx->PSSetSamplers(0, 1, &m_sampler);
        m_ctx->Draw(6, 0);

        ID3D11ShaderResourceView* nullSrv = nullptr;
        m_ctx->PSSetShaderResources(0, 1, &nullSrv);

        srv->Release();
        m_swap->Present(0, 0);
    }

    if (keyedMutex)
    {
        keyedMutex->ReleaseSync(0);
        keyedMutex->Release();
    }

    sharedTex->Release();
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
