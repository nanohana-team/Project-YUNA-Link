#pragma once
// Project YUNA Link - pose_server.h

#include "driver_main.h"
#include <windows.h>
#include <thread>
#include <mutex>
#include <atomic>
#include <vector>
#include <cstdint>

#define YUNA_PIPE_NAME R"(\\.\pipe\YunaLinkPose)"

// ---------------------------------------------------------------------------
// Wire protocol  (must match apps/pose_sender.py exactly)
// ---------------------------------------------------------------------------

#pragma pack(push, 1)

enum class YunaDeviceType : uint8_t
{
    HMD       = 0,
    CtrlLeft  = 1,
    CtrlRight = 2,
};

enum class YunaPacketType : uint8_t
{
    Pose  = 0x01,
    Input = 0x02,
};

struct YunaPacketHeader     // 3 bytes
{
    YunaPacketType type;
    uint16_t       length;
};

struct YunaPosePacket       // 57 bytes
{
    YunaDeviceType device;
    double px, py, pz;
    double qw, qx, qy, qz;
};

struct YunaInputPacket      // 16 bytes
{
    YunaDeviceType device;
    bool  triggerClick;
    bool  gripClick;
    bool  aClick;
    bool  bClick;
    float triggerValue;
    float joystickX;
    float joystickY;
};

#pragma pack(pop)

// ---------------------------------------------------------------------------
// Controller input snapshot
// ---------------------------------------------------------------------------
struct ControllerInput
{
    bool  triggerClick = false;
    bool  gripClick    = false;
    bool  aClick       = false;
    bool  bClick       = false;
    float triggerValue = 0.f;
    float joystickX    = 0.f;
    float joystickY    = 0.f;
};

// ---------------------------------------------------------------------------
// PoseServer
// ---------------------------------------------------------------------------
class PoseServer
{
public:
    PoseServer();
    ~PoseServer();

    void Start();
    void Stop();

    bool HasHMDPose()             const;
    bool HasLeftControllerPose()  const;
    bool HasRightControllerPose() const;

    vr::DriverPose_t GetHMDPose()             const;
    vr::DriverPose_t GetLeftControllerPose()  const;
    vr::DriverPose_t GetRightControllerPose() const;

    ControllerInput  GetLeftInput()  const;
    ControllerInput  GetRightInput() const;

private:
    void ServerThread();
    bool ReadExact(HANDLE hPipe, void* buf, DWORD bytes);
    void ApplyPosePacket (const YunaPosePacket&  pkt);
    void ApplyInputPacket(const YunaInputPacket& pkt);

    static vr::DriverPose_t BuildPose(const YunaPosePacket& pkt);
    static vr::DriverPose_t DefaultPose(double x, double y, double z);

    std::thread        m_thread;
    std::atomic<bool>  m_running{ false };

    // Stop() signals the server thread via this event instead of a
    // dummy pipe connection, avoiding potential deadlock.
    HANDLE             m_stopEvent = INVALID_HANDLE_VALUE;

    mutable std::mutex m_mutex;

    vr::DriverPose_t m_hmdPose{};
    vr::DriverPose_t m_leftPose{};
    vr::DriverPose_t m_rightPose{};

    bool m_hasHMD   = false;
    bool m_hasLeft  = false;
    bool m_hasRight = false;

    ControllerInput m_leftInput{};
    ControllerInput m_rightInput{};
};
