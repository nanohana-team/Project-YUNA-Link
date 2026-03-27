#pragma once
// Project YUNA Link - controller_device.h

#include "driver_main.h"
#include "pose_server.h"
#include <string>

class YunaController : public vr::ITrackedDeviceServerDriver
{
public:
    YunaController(vr::ETrackedControllerRole role, PoseServer* poseServer);

    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void             Deactivate() override;
    void             EnterStandby() override;
    void*            GetComponent(const char* pchComponentNameAndVersion) override;
    void             DebugRequest(const char* pchRequest,
                                  char* pchResponseBuffer,
                                  uint32_t unResponseBufferSize) override;
    vr::DriverPose_t GetPose() override;

    void        RunFrame();
    const char* GetSerialNumber() const { return m_serial.c_str(); }

private:
    bool IsLeft() const { return m_role == vr::TrackedControllerRole_LeftHand; }

    uint32_t                   m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
    vr::ETrackedControllerRole m_role;
    PoseServer*                m_poseServer;
    std::string                m_serial;
    vr::DriverPose_t           m_defaultPose{};

    vr::VRInputComponentHandle_t m_triggerClick = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_gripClick    = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_systemClick  = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_aClick       = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_bClick       = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_triggerValue = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_joystickX    = vr::k_ulInvalidInputComponentHandle;
    vr::VRInputComponentHandle_t m_joystickY    = vr::k_ulInvalidInputComponentHandle;

    void InitDefaultPose();
};
