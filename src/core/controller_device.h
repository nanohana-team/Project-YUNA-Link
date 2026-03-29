#pragma once
// Project YUNA Link - controller_device.h
// Presents as Oculus Touch. Input paths match SteamVR oculus_touch binding:
//   joystick (not thumbstick), grip (not squeeze)

#include "driver_main.h"
#include "shared_state.h"
#include <string>

class YunaController : public vr::ITrackedDeviceServerDriver
{
public:
    YunaController(vr::ETrackedControllerRole role, SharedState* state);

    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void             Deactivate() override;
    void             EnterStandby() override;
    void*            GetComponent(const char*) override;
    void             DebugRequest(const char*, char*, uint32_t) override;
    vr::DriverPose_t GetPose() override;

    void        RunFrame();
    const char* GetSerialNumber() const { return m_serial.c_str(); }

private:
    bool IsLeft() const { return m_role == vr::TrackedControllerRole_LeftHand; }

    uint32_t                   m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
    vr::ETrackedControllerRole m_role;
    SharedState*               m_state;
    std::string                m_serial;

    // Both hands
    vr::VRInputComponentHandle_t m_system      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_appMenu     = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_triggerVal  = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_triggerClk  = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_triggerTouch= vr::k_ulInvalidInputComponentHandle;
    // grip (was squeeze)
    vr::VRInputComponentHandle_t m_gripVal     = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_gripClk     = vr::k_ulInvalidInputComponentHandle;
    // joystick (was thumbstick)
    vr::VRInputComponentHandle_t m_joyX        = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_joyY        = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_joyClick    = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_joyTouch    = vr::k_ulInvalidInputComponentHandle;

    // Left only: X, Y
    vr::VRInputComponentHandle_t m_xClick      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_yClick      = vr::k_ulInvalidInputComponentHandle;
    // Right only: A, B
    vr::VRInputComponentHandle_t m_aClick      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_bClick      = vr::k_ulInvalidInputComponentHandle;
};
