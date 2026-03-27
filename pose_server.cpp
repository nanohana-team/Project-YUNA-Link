// Project YUNA Link - pose_server.cpp

#include "driver_main.h"
#include "pose_server.h"

// ---------------------------------------------------------------------------
// Static helpers
// ---------------------------------------------------------------------------
vr::DriverPose_t PoseServer::DefaultPose(double x, double y, double z)
{
    vr::DriverPose_t p{};
    p.poseIsValid                = false;
    p.deviceIsConnected          = true;
    p.result                     = vr::TrackingResult_Running_OK;
    p.vecPosition[0]             = x;
    p.vecPosition[1]             = y;
    p.vecPosition[2]             = z;
    p.qRotation.w                = 1.0;
    p.qWorldFromDriverRotation.w = 1.0;
    p.qDriverFromHeadRotation.w  = 1.0;
    return p;
}

vr::DriverPose_t PoseServer::BuildPose(const YunaPosePacket& pkt)
{
    vr::DriverPose_t p{};
    p.poseIsValid                = true;
    p.result                     = vr::TrackingResult_Running_OK;
    p.deviceIsConnected          = true;
    p.vecPosition[0]             = pkt.px;
    p.vecPosition[1]             = pkt.py;
    p.vecPosition[2]             = pkt.pz;
    p.qRotation.w                = pkt.qw;
    p.qRotation.x                = pkt.qx;
    p.qRotation.y                = pkt.qy;
    p.qRotation.z                = pkt.qz;
    p.qWorldFromDriverRotation.w = 1.0;
    p.qDriverFromHeadRotation.w  = 1.0;
    return p;
}

// ---------------------------------------------------------------------------
// Constructor / Destructor
// ---------------------------------------------------------------------------
PoseServer::PoseServer()
{
    m_hmdPose   = DefaultPose( 0.0,  1.6,  0.0);
    m_leftPose  = DefaultPose(-0.25, 1.1, -0.1);
    m_rightPose = DefaultPose( 0.25, 1.1, -0.1);

    // Manual-reset event used to signal the server thread to exit
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

// ---------------------------------------------------------------------------
// Start / Stop
// ---------------------------------------------------------------------------
void PoseServer::Start()
{
    if (m_running.exchange(true)) return;   // already running
    ResetEvent(m_stopEvent);
    m_thread = std::thread(&PoseServer::ServerThread, this);
}

void PoseServer::Stop()
{
    if (!m_running.exchange(false)) return; // already stopped

    // Signal the server thread to exit - no dummy pipe connection needed
    if (m_stopEvent != INVALID_HANDLE_VALUE)
        SetEvent(m_stopEvent);

    if (m_thread.joinable())
        m_thread.join();
}

// ---------------------------------------------------------------------------
// ReadExact: read exactly `bytes` bytes, return false on error / stop signal
// Uses overlapped I/O so we can respect m_stopEvent
// ---------------------------------------------------------------------------
bool PoseServer::ReadExact(HANDLE hPipe, void* buf, DWORD bytes)
{
    DWORD total = 0;
    while (total < bytes)
    {
        OVERLAPPED ov{};
        ov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
        if (ov.hEvent == nullptr) return false;

        DWORD read = 0;
        BOOL ok = ReadFile(hPipe,
                           static_cast<BYTE*>(buf) + total,
                           bytes - total, &read, &ov);

        if (!ok && GetLastError() == ERROR_IO_PENDING)
        {
            // Wait for either data or stop signal
            HANDLE handles[2] = { ov.hEvent, m_stopEvent };
            DWORD waited = WaitForMultipleObjects(2, handles, FALSE, INFINITE);

            if (waited == WAIT_OBJECT_0)
            {
                // Data arrived
                ok = GetOverlappedResult(hPipe, &ov, &read, FALSE);
            }
            else
            {
                // Stop event or error
                CancelIo(hPipe);
                CloseHandle(ov.hEvent);
                return false;
            }
        }

        CloseHandle(ov.hEvent);

        if (!ok || read == 0) return false;
        total += read;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Server thread: one client at a time, overlapped pipe
// ---------------------------------------------------------------------------
void PoseServer::ServerThread()
{
    while (m_running)
    {
        // Create pipe in overlapped mode so ConnectNamedPipe is non-blocking
        HANDLE hPipe = CreateNamedPipeA(
            YUNA_PIPE_NAME,
            PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,      // only one instance needed
            0, 4096, 0, nullptr);

        if (hPipe == INVALID_HANDLE_VALUE)
        {
            DriverLog("[YUNA] CreateNamedPipe failed (%lu)\n", GetLastError());
            // Wait a second or until stop, then retry
            WaitForSingleObject(m_stopEvent, 1000);
            continue;
        }

        // Overlapped connect - wait for client or stop event
        OVERLAPPED ov{};
        ov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);

        BOOL connected = ConnectNamedPipe(hPipe, &ov);
        DWORD err = GetLastError();

        if (!connected)
        {
            if (err == ERROR_IO_PENDING)
            {
                HANDLE handles[2] = { ov.hEvent, m_stopEvent };
                DWORD waited = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
                if (waited != WAIT_OBJECT_0)
                {
                    // Stop requested while waiting for client
                    CancelIo(hPipe);
                    CloseHandle(ov.hEvent);
                    CloseHandle(hPipe);
                    break;
                }
                DWORD dummy;
                connected = GetOverlappedResult(hPipe, &ov, &dummy, FALSE);
            }
            else if (err == ERROR_PIPE_CONNECTED)
            {
                connected = TRUE;
            }
        }

        CloseHandle(ov.hEvent);

        if (connected && m_running)
        {
            DriverLog("[YUNA] Client connected\n");

            // Read packets until disconnect or stop
            while (m_running)
            {
                YunaPacketHeader hdr{};
                if (!ReadExact(hPipe, &hdr, sizeof(hdr))) break;

                if (hdr.type == YunaPacketType::Pose
                    && hdr.length == sizeof(YunaPosePacket))
                {
                    YunaPosePacket pkt{};
                    if (!ReadExact(hPipe, &pkt, sizeof(pkt))) break;
                    ApplyPosePacket(pkt);
                }
                else if (hdr.type == YunaPacketType::Input
                         && hdr.length == sizeof(YunaInputPacket))
                {
                    YunaInputPacket pkt{};
                    if (!ReadExact(hPipe, &pkt, sizeof(pkt))) break;
                    ApplyInputPacket(pkt);
                }
                else
                {
                    // Unknown packet: skip payload
                    std::vector<uint8_t> skip(hdr.length);
                    if (!ReadExact(hPipe, skip.data(), hdr.length)) break;
                }
            }

            DriverLog("[YUNA] Client disconnected\n");
        }

        DisconnectNamedPipe(hPipe);
        CloseHandle(hPipe);
    }

    DriverLog("[YUNA] PoseServer thread exiting\n");
}

// ---------------------------------------------------------------------------
// Packet application
// ---------------------------------------------------------------------------
void PoseServer::ApplyPosePacket(const YunaPosePacket& pkt)
{
    std::lock_guard<std::mutex> lk(m_mutex);
    switch (pkt.device)
    {
    case YunaDeviceType::HMD:
        m_hmdPose   = BuildPose(pkt); m_hasHMD   = true; break;
    case YunaDeviceType::CtrlLeft:
        m_leftPose  = BuildPose(pkt); m_hasLeft  = true; break;
    case YunaDeviceType::CtrlRight:
        m_rightPose = BuildPose(pkt); m_hasRight = true; break;
    default: break;
    }
}

void PoseServer::ApplyInputPacket(const YunaInputPacket& pkt)
{
    ControllerInput in;
    in.triggerClick = pkt.triggerClick;
    in.gripClick    = pkt.gripClick;
    in.aClick       = pkt.aClick;
    in.bClick       = pkt.bClick;
    in.triggerValue = pkt.triggerValue;
    in.joystickX    = pkt.joystickX;
    in.joystickY    = pkt.joystickY;

    std::lock_guard<std::mutex> lk(m_mutex);
    if (pkt.device == YunaDeviceType::CtrlLeft) m_leftInput  = in;
    else                                         m_rightInput = in;
}

// ---------------------------------------------------------------------------
// Getters
// ---------------------------------------------------------------------------
bool PoseServer::HasHMDPose()             const { std::lock_guard<std::mutex> l(m_mutex); return m_hasHMD;   }
bool PoseServer::HasLeftControllerPose()  const { std::lock_guard<std::mutex> l(m_mutex); return m_hasLeft;  }
bool PoseServer::HasRightControllerPose() const { std::lock_guard<std::mutex> l(m_mutex); return m_hasRight; }

vr::DriverPose_t PoseServer::GetHMDPose()             const { std::lock_guard<std::mutex> l(m_mutex); return m_hmdPose;   }
vr::DriverPose_t PoseServer::GetLeftControllerPose()  const { std::lock_guard<std::mutex> l(m_mutex); return m_leftPose;  }
vr::DriverPose_t PoseServer::GetRightControllerPose() const { std::lock_guard<std::mutex> l(m_mutex); return m_rightPose; }

ControllerInput  PoseServer::GetLeftInput()  const { std::lock_guard<std::mutex> l(m_mutex); return m_leftInput;  }
ControllerInput  PoseServer::GetRightInput() const { std::lock_guard<std::mutex> l(m_mutex); return m_rightInput; }
