#pragma once
// Project YUNA Link - controller_device.h

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

    vr::VRInputComponentHandle_t m_startClick  = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_aClick      = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_thumbstickX = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_thumbstickY = vr::k_ulInvalidInputComponentHandle;
};
