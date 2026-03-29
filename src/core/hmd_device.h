#pragma once
// Project YUNA Link - hmd_device.h

#include "driver_main.h"
#include "shared_state.h"
#include "display_window.h"
#include <memory>

class YunaHMD
    : public vr::ITrackedDeviceServerDriver
    , public vr::IVRDisplayComponent
    , public vr::IVRVirtualDisplay
{
public:
    explicit YunaHMD(SharedState* state);

    // ITrackedDeviceServerDriver
    vr::EVRInitError Activate(uint32_t unObjectId) override;
    void             Deactivate() override;
    void             EnterStandby() override;
    void*            GetComponent(const char* pchNameAndVersion) override;
    void             DebugRequest(const char*, char*, uint32_t) override;
    vr::DriverPose_t GetPose() override;

    // IVRDisplayComponent_003
    void GetWindowBounds(int32_t*, int32_t*, uint32_t*, uint32_t*) override;
    bool IsDisplayOnDesktop()   override;
    bool IsDisplayRealDisplay() override;
    void GetRecommendedRenderTargetSize(uint32_t*, uint32_t*) override;
    void GetEyeOutputViewport(vr::EVREye, uint32_t*, uint32_t*,
                              uint32_t*, uint32_t*) override;
    void GetProjectionRaw(vr::EVREye, float*, float*, float*, float*) override;
    vr::DistortionCoordinates_t ComputeDistortion(vr::EVREye, float, float) override;
    bool ComputeInverseDistortion(vr::HmdVector2_t*, vr::EVREye,
                                  uint32_t, float, float) override;

    // IVRVirtualDisplay_002
    void Present(const vr::PresentInfo_t*, uint32_t) override;
    void WaitForPresent() override;
    bool GetTimeSinceLastVsync(float*, uint64_t*) override;

    void        RunFrame();
    const char* GetSerialNumber() const { return "YUNA_HMD_001"; }

    static constexpr uint32_t EYE_W = 1920;
    static constexpr uint32_t EYE_H = 1920;

private:
    uint32_t     m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
    SharedState* m_state;
    std::unique_ptr<DisplayWindow> m_dispWindow;

    void InitDefaultPose();
    vr::DriverPose_t m_defaultPose{};
};
