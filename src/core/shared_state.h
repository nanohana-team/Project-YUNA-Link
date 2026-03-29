#pragma once
// Project YUNA Link - shared_state.h

#include <openvr_driver.h>
#include <mutex>
#include <chrono>
#include <cmath>

// ---------------------------------------------------------------------------
// HandInputState
// ---------------------------------------------------------------------------
struct HandInputState
{
    bool  aButton      = false;
    bool  bButton      = false;
    bool  xButton      = false;
    bool  yButton      = false;
    float triggerValue = 0.f;
    float gripValue    = 0.f;
    float stickX       = 0.f;
    float stickY       = 0.f;
};

struct GlobalInputState
{
    bool           startButton = false;
    bool           menuButton  = false;
    HandInputState left;
    HandInputState right;

    void reset()
    {
        startButton = menuButton = false;
        left  = HandInputState{};
        right = HandInputState{};
    }

    static float clamp(float v)   { return v<-1.f?-1.f:v>1.f?1.f:v; }
    static float clamp01(float v) { return v<0.f?0.f:v>1.f?1.f:v; }
};

// ---------------------------------------------------------------------------
// ControllerState
// ---------------------------------------------------------------------------
struct ControllerState
{
    vr::DriverPose_t pose{};
    HandInputState   input;
    bool             hasPose = false;
};

// ---------------------------------------------------------------------------
// PoseOverride
// Stores 6DOF state for command-driven pose control.
//
// MOVE  target axis delta  -> relative position  (add delta)
// ROTATE target axis delta -> relative rotation  (add delta degrees)
// SET   target axis value  -> absolute position
// SET   target rX/rY/rZ v  -> absolute rotation (degrees)
// RESET_POSE target        -> deactivate override
//
// Targets: HEAD  L_CONTROLLER  R_CONTROLLER
// Axes:    x y z  (position)   rX rY rZ  (rotation, degrees)
// ---------------------------------------------------------------------------
struct PoseOverride
{
    // Position (metres)
    double px = 0., py = 0., pz = 0.;

    // Euler angles (degrees) -- YXZ convention
    double yaw   = 0.;  // ry  Y-axis
    double pitch = 0.;  // rx  X-axis
    double roll  = 0.;  // rz  Z-axis

    bool active = false;

    void initDefault(double dx, double dy, double dz)
    {
        px=dx; py=dy; pz=dz;
        yaw=pitch=roll=0.;
        active=false;
    }

    vr::DriverPose_t toPose() const
    {
        vr::DriverPose_t p{};
        p.poseIsValid                = true;
        p.result                     = vr::TrackingResult_Running_OK;
        p.deviceIsConnected          = true;
        p.vecPosition[0]             = px;
        p.vecPosition[1]             = py;
        p.vecPosition[2]             = pz;
        p.qWorldFromDriverRotation.w = 1.0;
        p.qDriverFromHeadRotation.w  = 1.0;

        // Euler (degrees) -> quaternion  YXZ
        static const double D2R = 3.14159265358979323846 / 180.0;
        double hy = yaw*D2R/2., hx = pitch*D2R/2., hz = roll*D2R/2.;
        double cy=cos(hy),sy=sin(hy);
        double cx=cos(hx),sx=sin(hx);
        double cz=cos(hz),sz=sin(hz);
        p.qRotation.w =  cy*cx*cz + sy*sx*sz;
        p.qRotation.x =  cy*sx*cz + sy*cx*sz;
        p.qRotation.y =  sy*cx*cz - cy*sx*sz;
        p.qRotation.z =  cy*cx*sz - sy*sx*cz;
        return p;
    }
};

// ---------------------------------------------------------------------------
// SharedState
// ---------------------------------------------------------------------------
class SharedState
{
public:
    using Clock = std::chrono::steady_clock;
    using TP    = Clock::time_point;

    SharedState()
    {
        m_hmdPose        = _defPose( 0.0,  1.6,  0.0);
        m_leftCtrl.pose  = _defPose(-0.25, 1.1, -0.1);
        m_rightCtrl.pose = _defPose( 0.25, 1.1, -0.1);
        m_ovHead.initDefault( 0.0,  1.6,  0.0);
        m_ovLeft.initDefault(-0.25, 1.1, -0.1);
        m_ovRight.initDefault( 0.25, 1.1, -0.1);
    }

    // ------------------------------------------------------------------
    // HMD pose (from PoseServer binary packet)
    // ------------------------------------------------------------------
    void setHmdPose(const vr::DriverPose_t& p)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_hmdPose=p; m_hasHmd=true; m_lastRecv=Clock::now();
    }

    bool hasHmdPose() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        return m_hasHmd || m_ovHead.active;
    }

    vr::DriverPose_t getHmdPose() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        if (m_ovHead.active) return m_ovHead.toPose();
        return m_hmdPose;
    }

    // ------------------------------------------------------------------
    // FramePacket controller poses + inputs
    // ------------------------------------------------------------------
    void setFrame(const ControllerState& left, const ControllerState& right,
                  const GlobalInputState& input)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        m_leftCtrl=left; m_rightCtrl=right; m_input=input;
        m_lastRecv=Clock::now();
    }

    ControllerState getLeft() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        ControllerState cs = m_leftCtrl;
        if (m_ovLeft.active) { cs.pose=m_ovLeft.toPose(); cs.hasPose=true; }
        return cs;
    }

    ControllerState getRight() const
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        ControllerState cs = m_rightCtrl;
        if (m_ovRight.active) { cs.pose=m_ovRight.toPose(); cs.hasPose=true; }
        return cs;
    }

    GlobalInputState getInput() const
    {
        std::lock_guard<std::mutex> lk(m_mtx); return m_input;
    }

    // ------------------------------------------------------------------
    // MOVE: relative position delta  (device: 0=HEAD 1=LEFT 2=RIGHT)
    // ------------------------------------------------------------------
    void cmdMove(int device, int axis, double delta)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        PoseOverride& ov = _ov(device);
        ov.active = true;
        switch(axis){ case 0: ov.px+=delta; break; case 1: ov.py+=delta; break; default: ov.pz+=delta; }
    }

    // ------------------------------------------------------------------
    // ROTATE: relative rotation delta in degrees  (axis: 0=X 1=Y 2=Z)
    // ------------------------------------------------------------------
    void cmdRotate(int device, int axis, double delta)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        PoseOverride& ov = _ov(device);
        ov.active = true;
        switch(axis){ case 0: ov.pitch+=delta; break; case 1: ov.yaw+=delta; break; default: ov.roll+=delta; }
    }

    // ------------------------------------------------------------------
    // SET: absolute position (axis 0=x 1=y 2=z)
    //      or absolute rotation (axis 3=rx/pitch 4=ry/yaw 5=rz/roll)
    // ------------------------------------------------------------------
    void cmdSetPose(int device, int axis, double value)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        PoseOverride& ov = _ov(device);
        ov.active = true;
        switch(axis)
        {
        case 0: ov.px    = value; break;
        case 1: ov.py    = value; break;
        case 2: ov.pz    = value; break;
        case 3: ov.pitch = value; break;
        case 4: ov.yaw   = value; break;
        case 5: ov.roll  = value; break;
        }
    }

    // ------------------------------------------------------------------
    // RESET_POSE: deactivate override for one device
    // ------------------------------------------------------------------
    void cmdResetPose(int device)
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        _ov(device).active = false;
    }

    // ------------------------------------------------------------------
    // Input overrides
    // ------------------------------------------------------------------
    void cmdSetStart(bool v)    { std::lock_guard<std::mutex> lk(m_mtx); m_input.startButton=v; }
    void cmdSetMenu(bool v)     { std::lock_guard<std::mutex> lk(m_mtx); m_input.menuButton =v; }
    void cmdSetA(bool v)        { std::lock_guard<std::mutex> lk(m_mtx); m_input.right.aButton=v; }
    void cmdSetB(bool v)        { std::lock_guard<std::mutex> lk(m_mtx); m_input.right.bButton=v; }
    void cmdSetX(bool v)        { std::lock_guard<std::mutex> lk(m_mtx); m_input.left.xButton =v; }
    void cmdSetY(bool v)        { std::lock_guard<std::mutex> lk(m_mtx); m_input.left.yButton  =v; }
    void cmdSetRTrigger(float v){ std::lock_guard<std::mutex> lk(m_mtx); m_input.right.triggerValue=GlobalInputState::clamp01(v); }
    void cmdSetRGrip(float v)   { std::lock_guard<std::mutex> lk(m_mtx); m_input.right.gripValue   =GlobalInputState::clamp01(v); }
    void cmdSetLTrigger(float v){ std::lock_guard<std::mutex> lk(m_mtx); m_input.left.triggerValue =GlobalInputState::clamp01(v); }
    void cmdSetLGrip(float v)   { std::lock_guard<std::mutex> lk(m_mtx); m_input.left.gripValue    =GlobalInputState::clamp01(v); }
    void cmdSetLeftStick(float x,float y)
    { std::lock_guard<std::mutex> lk(m_mtx);
      m_input.left.stickX=GlobalInputState::clamp(x); m_input.left.stickY=GlobalInputState::clamp(y); }
    void cmdSetRightStick(float x,float y)
    { std::lock_guard<std::mutex> lk(m_mtx);
      m_input.right.stickX=GlobalInputState::clamp(x); m_input.right.stickY=GlobalInputState::clamp(y); }

    // Reset input only (pose overrides preserved)
    void cmdReset()      { std::lock_guard<std::mutex> lk(m_mtx); m_input.reset(); }
    void resetLastRecv() { std::lock_guard<std::mutex> lk(m_mtx); m_lastRecv=Clock::now(); }

    // ------------------------------------------------------------------
    // Failsafe: 250ms no packet -> tracking invalid
    // ------------------------------------------------------------------
    void applyFailsafe()
    {
        std::lock_guard<std::mutex> lk(m_mtx);
        // Only apply to non-overridden devices
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      Clock::now()-m_lastRecv).count();
        if (ms > 250)
        {
            if (!m_ovHead.active)  m_hmdPose.poseIsValid        = false;
            if (!m_ovLeft.active)  m_leftCtrl.pose.poseIsValid  = false;
            if (!m_ovRight.active) m_rightCtrl.pose.poseIsValid = false;
        }
    }

private:
    PoseOverride& _ov(int d)
    {
        if (d==1) return m_ovLeft;
        if (d==2) return m_ovRight;
        return m_ovHead;
    }

    static vr::DriverPose_t _defPose(double x, double y, double z)
    {
        vr::DriverPose_t p{};
        p.poseIsValid=false; p.deviceIsConnected=true;
        p.result=vr::TrackingResult_Running_OK;
        p.vecPosition[0]=x; p.vecPosition[1]=y; p.vecPosition[2]=z;
        p.qRotation.w=p.qWorldFromDriverRotation.w=p.qDriverFromHeadRotation.w=1.0;
        return p;
    }

    mutable std::mutex m_mtx;
    vr::DriverPose_t   m_hmdPose{};
    ControllerState    m_leftCtrl{};
    ControllerState    m_rightCtrl{};
    GlobalInputState   m_input{};
    bool               m_hasHmd   = false;
    TP                 m_lastRecv = Clock::now();

    // Command-driven pose overrides (independent of FramePacket)
    PoseOverride m_ovHead{};
    PoseOverride m_ovLeft{};
    PoseOverride m_ovRight{};
};
