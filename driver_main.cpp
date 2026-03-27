// Project YUNA Link - driver_main.cpp

#include "driver_main.h"
#include "hmd_device.h"
#include "controller_device.h"
#include "pose_server.h"

static YunaDriverProvider g_provider;

extern "C" __declspec(dllexport)
void* HmdDriverFactory(const char* pInterfaceName, int* pReturnCode)
{
    if (strcmp(pInterfaceName, vr::IServerTrackedDeviceProvider_Version) == 0)
        return &g_provider;

    if (pReturnCode)
        *pReturnCode = vr::VRInitError_Init_InterfaceNotFound;
    return nullptr;
}

vr::EVRInitError YunaDriverProvider::Init(vr::IVRDriverContext* pDriverContext)
{
    VR_INIT_SERVER_DRIVER_CONTEXT(pDriverContext);
    DriverLog("[YUNA] Driver initializing\n");

    m_poseServer = std::make_unique<PoseServer>();
    m_poseServer->Start();

    m_hmd = std::make_shared<YunaHMD>(m_poseServer.get());
    vr::VRServerDriverHost()->TrackedDeviceAdded(
        m_hmd->GetSerialNumber(),
        vr::TrackedDeviceClass_HMD,
        m_hmd.get());

    m_ctrlLeft = std::make_shared<YunaController>(
        vr::TrackedControllerRole_LeftHand, m_poseServer.get());
    vr::VRServerDriverHost()->TrackedDeviceAdded(
        m_ctrlLeft->GetSerialNumber(),
        vr::TrackedDeviceClass_Controller,
        m_ctrlLeft.get());

    m_ctrlRight = std::make_shared<YunaController>(
        vr::TrackedControllerRole_RightHand, m_poseServer.get());
    vr::VRServerDriverHost()->TrackedDeviceAdded(
        m_ctrlRight->GetSerialNumber(),
        vr::TrackedDeviceClass_Controller,
        m_ctrlRight.get());

    DriverLog("[YUNA] Driver initialized: HMD + 2 Controllers\n");
    return vr::VRInitError_None;
}

void YunaDriverProvider::Cleanup()
{
    DriverLog("[YUNA] Driver cleanup\n");
    if (m_poseServer) m_poseServer->Stop();
    VR_CLEANUP_SERVER_DRIVER_CONTEXT();
}

const char* const* YunaDriverProvider::GetInterfaceVersions()
{
    return vr::k_InterfaceVersions;
}

void YunaDriverProvider::RunFrame()
{
    if (m_hmd)       m_hmd->RunFrame();
    if (m_ctrlLeft)  m_ctrlLeft->RunFrame();
    if (m_ctrlRight) m_ctrlRight->RunFrame();
}

bool YunaDriverProvider::ShouldBlockStandbyMode() { return false; }
void YunaDriverProvider::EnterStandby() {}
void YunaDriverProvider::LeaveStandby() {}
