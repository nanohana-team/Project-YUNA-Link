#pragma once
// Project YUNA Link - shared_state.h
// Thread-safe internal state buffers.

#include <openvr_driver.h>
#include <mutex>
#include <chrono>
#include <algorithm>

// ---------------------------------------------------------------------------
// HandInputState / GlobalInputState  (spec: architecture.md)
// ---------------------------------------------------------------------------
struct HandInputState
{
    bool  aButton = false;
    float stickX  = 0.f;
    float stickY  = 0.f;
};

struct GlobalInputState
{
    bool           startButton = false;
    HandInputState left;
    HandInputState right;

    void reset()
    {
        startButton     = false;
        left.aButton    = false;
        left.stickX     = 0.f;
        left.stickY     = 0.f;
        right.aButton   = false;
        right.stickX    = 0.f;
        right.stickY    = 0.f;
    }

    static float clamp(float v)
    {
        return v < -1.f ? -1.f : v > 1.f ? 1.f : v;
    }
};

// ---------------------------------------------------------------------------
// ControllerState: pose + input per hand
// ---------------------------------------------------------------------------
struct ControllerState
{
    vr::DriverPose_t pose{};
    HandInputState   input;
    bool             hasPose = false;
};

// ---------------------------------------------------------------------------
// SharedState: all mutable driver state, protected by one mutex
// ---------------------------------------------------------------------------
class SharedState
{
public:
    using Clock = std::chrono::steady_clock;
    using TP    = Clock::time_point;

    SharedState()
    {
        m_hmdPose   = defaultPose( 0.0,  1.6,  0.0);
        m_leftCtrl  = {};
        m_leftCtrl.pose  = defaultPose(-0.25, 1.1, -0.1);
        m_rightCtrl = {};
        m_rightCtrl.pose = defaultPose( 0.25, 1.1, -0.1);
    }

    // ------------------------------------------------------------------
    // HMD pose
    // ------------------------------------------------------------------
    void setHmdPose(const vr::DriverPose_t& p)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_hmdPose   = p;
        m_hasHmd    = true;
        m_lastRecv  = Clock::now();
    }

    vr::DriverPose_t getHmdPose() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        return m_hmdPose;
    }

    bool hasHmdPose() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        return m_hasHmd;
    }

    // ------------------------------------------------------------------
    // FramePacket (controller poses + inputs)
    // ------------------------------------------------------------------
    void setFrame(const ControllerState& left,
                  const ControllerState& right,
                  const GlobalInputState& input)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_leftCtrl  = left;
        m_rightCtrl = right;
        m_input     = input;
        m_lastRecv  = Clock::now();
    }

    ControllerState getLeft()  const
    {
        std::lock_guard<std::mutex> lk(m_mtx); return m_leftCtrl;
    }
    ControllerState getRight() const
    {
        std::lock_guard<std::mutex> lk(m_mtx); return m_rightCtrl;
    }
    GlobalInputState getInput() const
    {
        std::lock_guard<std::mutex> lk(m_mtx); return m_input;
    }

    // ------------------------------------------------------------------
    // InputServer overrides (SET/TAP/RESET commands)
    // These take priority over FramePacket input when set.
    // ------------------------------------------------------------------
    void cmdSetStart(bool v)
    {
        std::lock_guard<std::mutex> lk(m_mtx); m_input.startButton = v;
    }
    void cmdSetA(bool v)
    {
        std::lock_guard<std::mutex> lk(m_mtx); m_input.right.aButton = v;
    }
    void cmdSetLeftStick(float x, float y)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_input.left.stickX = GlobalInputState::clamp(x);
        m_input.left.stickY = GlobalInputState::clamp(y);
    }
    void cmdSetRightStick(float x, float y)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_input.right.stickX = GlobalInputState::clamp(x);
        m_input.right.stickY = GlobalInputState::clamp(y);
    }
    void cmdReset()
    {
        std::lock_guard<std::mutex> lk(m_mtx); m_input.reset();
    }

    // ------------------------------------------------------------------
    // Failsafe: 250ms no packet -> mark tracking invalid
    // ------------------------------------------------------------------
    void applyFailsafe()
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        auto now = Clock::now();
        auto ms  = std::chrono::duration_cast<std::chrono::milliseconds>(
                       now - m_lastRecv).count();
        if (ms > 250)
        {
            m_hmdPose.poseIsValid        = false;
            m_leftCtrl.pose.poseIsValid  = false;
            m_rightCtrl.pose.poseIsValid = false;
        }
    }

    void resetLastRecv()
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_lastRecv = Clock::now();
    }

private:
    static vr::DriverPose_t defaultPose(double x, double y, double z)
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

    mutable std::mutex m_mtx;
    vr::DriverPose_t   m_hmdPose{};
    ControllerState    m_leftCtrl{};
    ControllerState    m_rightCtrl{};
    GlobalInputState   m_input{};
    bool               m_hasHmd   = false;
    TP                 m_lastRecv = Clock::now();
};
