// Project YUNA Link - pose_server.cpp

#include "driver_main.h"
#include "pose_server.h"
#include "protocol.h"
#include <cstring>

// ---------------------------------------------------------------------------
// Helpers: build DriverPose_t from packet data
// ---------------------------------------------------------------------------
static vr::DriverPose_t buildFromHmd(const HmdPosePacket& p)
{
    vr::DriverPose_t d{};
    d.poseIsValid                = true;
    d.result                     = vr::TrackingResult_Running_OK;
    d.deviceIsConnected          = true;
    d.vecPosition[0]             = p.px;
    d.vecPosition[1]             = p.py;
    d.vecPosition[2]             = p.pz;
    d.qRotation.w                = p.qw;
    d.qRotation.x                = p.qx;
    d.qRotation.y                = p.qy;
    d.qRotation.z                = p.qz;
    d.qWorldFromDriverRotation.w = 1.0;
    d.qDriverFromHeadRotation.w  = 1.0;
    return d;
}

static vr::DriverPose_t buildFromCtrl(const ControllerPoseData& cp)
{
    vr::DriverPose_t d{};
    d.poseIsValid                = cp.trackingValid != 0;
    d.result                     = vr::TrackingResult_Running_OK;
    d.deviceIsConnected          = cp.connected != 0;
    d.vecPosition[0]             = cp.px;
    d.vecPosition[1]             = cp.py;
    d.vecPosition[2]             = cp.pz;
    d.qRotation.x                = cp.qx;
    d.qRotation.y                = cp.qy;
    d.qRotation.z                = cp.qz;
    d.qRotation.w                = cp.qw;
    d.qWorldFromDriverRotation.w = 1.0;
    d.qDriverFromHeadRotation.w  = 1.0;
    return d;
}

// ---------------------------------------------------------------------------
// Constructor / Destructor
// ---------------------------------------------------------------------------
PoseServer::PoseServer(SharedState* state)
    : m_state(state)
{
    m_stopEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
}

PoseServer::~PoseServer()
{
    Stop();
    if (m_stopEvent != INVALID_HANDLE_VALUE)
    {
        CloseHandle(m_stopEvent);
        m_stopEvent = INVALID_HANDLE_VALUE;
    }
}

void PoseServer::Start()
{
    if (m_running.exchange(true)) return;
    ResetEvent(m_stopEvent);
    m_thread = std::thread(&PoseServer::ServerThread, this);
    DriverLog("[YUNA] PoseServer listening on %s\n", YUNA_PIPE_NAME);
}

void PoseServer::Stop()
{
    if (!m_running.exchange(false)) return;
    if (m_stopEvent != INVALID_HANDLE_VALUE) SetEvent(m_stopEvent);
    if (m_thread.joinable()) m_thread.join();
}

// ---------------------------------------------------------------------------
// ReadExact: overlapped read, respects m_stopEvent
// ---------------------------------------------------------------------------
bool PoseServer::ReadExact(HANDLE hPipe, void* buf, DWORD bytes)
{
    DWORD total = 0;
    while (total < bytes)
    {
        OVERLAPPED ov{};
        ov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
        if (!ov.hEvent) return false;

        DWORD read = 0;
        BOOL ok = ReadFile(hPipe,
                           static_cast<BYTE*>(buf) + total,
                           bytes - total, &read, &ov);
        if (!ok && GetLastError() == ERROR_IO_PENDING)
        {
            HANDLE h[2] = { ov.hEvent, m_stopEvent };
            if (WaitForMultipleObjects(2, h, FALSE, INFINITE) != WAIT_OBJECT_0)
            {
                CancelIo(hPipe);
                CloseHandle(ov.hEvent);
                return false;
            }
            ok = GetOverlappedResult(hPipe, &ov, &read, FALSE);
        }
        CloseHandle(ov.hEvent);
        if (!ok || read == 0) return false;
        total += read;
    }
    return true;
}

// ---------------------------------------------------------------------------
// ServerThread
// ---------------------------------------------------------------------------
void PoseServer::ServerThread()
{
    while (m_running)
    {
        HANDLE hPipe = CreateNamedPipeA(
            YUNA_PIPE_NAME,
            PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, 0, 4096, 0, nullptr);

        if (hPipe == INVALID_HANDLE_VALUE)
        {
            DriverLog("[YUNA] CreateNamedPipe failed (%lu)\n", GetLastError());
            WaitForSingleObject(m_stopEvent, 1000);
            continue;
        }

        // Wait for client
        OVERLAPPED ov{};
        ov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
        BOOL connected = ConnectNamedPipe(hPipe, &ov);
        DWORD err = GetLastError();

        if (!connected)
        {
            if (err == ERROR_IO_PENDING)
            {
                HANDLE h[2] = { ov.hEvent, m_stopEvent };
                if (WaitForMultipleObjects(2, h, FALSE, INFINITE) != WAIT_OBJECT_0)
                {
                    CancelIo(hPipe);
                    CloseHandle(ov.hEvent); CloseHandle(hPipe);
                    break;
                }
                DWORD d;
                connected = GetOverlappedResult(hPipe, &ov, &d, FALSE);
            }
            else if (err == ERROR_PIPE_CONNECTED)
            {
                connected = TRUE;
            }
        }
        CloseHandle(ov.hEvent);

        if (!connected || !m_running)
        {
            DisconnectNamedPipe(hPipe); CloseHandle(hPipe);
            continue;
        }

        DriverLog("[YUNA] Client connected\n");
        m_state->resetLastRecv();

        while (m_running)
        {
            PacketHeader hdr{};
            if (!ReadExact(hPipe, &hdr, sizeof(hdr))) break;

            std::vector<uint8_t> body(hdr.length);
            if (hdr.length > 0 && !ReadExact(hPipe, body.data(), hdr.length)) break;

            DispatchPacket(hdr.type, body);
        }

        // Disconnect: reset input for safety
        m_state->cmdReset();
        DriverLog("[YUNA] Client disconnected, state reset\n");

        DisconnectNamedPipe(hPipe);
        CloseHandle(hPipe);
    }
    DriverLog("[YUNA] PoseServer thread exiting\n");
}

// ---------------------------------------------------------------------------
// DispatchPacket
// ---------------------------------------------------------------------------
void PoseServer::DispatchPacket(uint8_t type, const std::vector<uint8_t>& body)
{
    if (type == PKT_HMD_POSE && body.size() == sizeof(HmdPosePacket))
    {
        HmdPosePacket p{};
        memcpy(&p, body.data(), sizeof(p));
        m_state->setHmdPose(buildFromHmd(p));
    }
    else if (type == PKT_FRAME && body.size() == sizeof(FramePacket))
    {
        FramePacket fp{};
        memcpy(&fp, body.data(), sizeof(fp));

        ControllerState left, right;
        left.pose     = buildFromCtrl(fp.leftPose);
        left.hasPose  = true;
        left.input.aButton = false;  // aButton is right-hand only per spec
        left.input.stickX  = fp.leftInput.stickX;
        left.input.stickY  = fp.leftInput.stickY;

        right.pose     = buildFromCtrl(fp.rightPose);
        right.hasPose  = true;
        right.input.aButton = fp.rightInput.aButton != 0;
        right.input.stickX  = fp.rightInput.stickX;
        right.input.stickY  = fp.rightInput.stickY;

        GlobalInputState gs{};
        gs.startButton   = fp.startButton != 0;
        gs.left          = left.input;
        gs.right         = right.input;

        m_state->setFrame(left, right, gs);
    }
    // else: unknown packet, silently skip
}
