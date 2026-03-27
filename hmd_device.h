#pragma once
// Project YUNA Link - hmd_device.h
//
// IVRVirtualDisplay is used for rendering output.
// IVRDisplayComponent is also returned from GetComponent so SteamVR
// can query display geometry, but actual pixel delivery goes through
// IVRVirtualDisplay::Present().

#include "driver_main.h"
#include "pose_server.h"
#include "display_window.h"
#include <memory>

class YunaHMD
    : public vr::ITrackedDeviceServerDriver
    , public vr::IVRDisplayComponent
    , public vr::IVRVirtualDisplay
{
public:
    explicit YunaHMD(PoseServer* poseServer);

    // ITrackedDeviceServerDriver
    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void             Deactivate() override;
    void             EnterStandby() override;
    void*            GetComponent(const char* pchNameAndVersion) override;
    void             DebugRequest(const char* pchRequest,
                                  char* pchResponseBuffer,
                                  uint32_t unResponseBufferSize) override;
    vr::DriverPose_t GetPose() override;

    // IVRDisplayComponent_003
    void GetWindowBounds(int32_t* pnX, int32_t* pnY,
                         uint32_t* pnWidth, uint32_t* pnHeight) override;
    bool IsDisplayOnDesktop()   override;
    bool IsDisplayRealDisplay() override;
    void GetRecommendedRenderTargetSize(uint32_t* pnWidth,
                                        uint32_t* pnHeight) override;
    void GetEyeOutputViewport(vr::EVREye eEye,
                              uint32_t* pnX, uint32_t* pnY,
                              uint32_t* pnWidth, uint32_t* pnHeight) override;
    void GetProjectionRaw(vr::EVREye eEye,
                          float* pfLeft, float* pfRight,
                          float* pfTop,  float* pfBottom) override;
    vr::DistortionCoordinates_t ComputeDistortion(vr::EVREye eEye,
                                                   float fU, float fV) override;
    bool ComputeInverseDistortion(vr::HmdVector2_t* pResult,
                                  vr::EVREye eEye, uint32_t unChannel,
                                  float fU, float fV) override;

    // IVRVirtualDisplay_002
    void Present(const vr::PresentInfo_t* pPresentInfo,
                 uint32_t unPresentInfoSize) override;
    void WaitForPresent() override;
    bool GetTimeSinceLastVsync(float* pfSecondsSinceLastVsync,
                               uint64_t* pulFrameCounter) override;

    void        RunFrame();
    const char* GetSerialNumber() const { return "YUNA_HMD_001"; }

    static constexpr uint32_t EYE_W = 1920;
    static constexpr uint32_t EYE_H = 1920;

private:
    uint32_t    m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
    PoseServer* m_poseServer;
    vr::DriverPose_t               m_defaultPose{};
    std::unique_ptr<DisplayWindow> m_dispWindow;

    void InitDefaultPose();
};
