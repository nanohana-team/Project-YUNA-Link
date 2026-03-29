#pragma once
// Project YUNA Link - driver_main.h

#include <openvr_driver.h>
#include <memory>
#include <cstdio>
#include <cstdarg>
#include <windows.h>

inline void DriverLog(const char* fmt, ...)
{
    char buf[1024];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    OutputDebugStringA(buf);
    auto* log = vr::VRDriverLog();
    if (log) log->Log(buf);
}

class YunaHMD;
class YunaController;
class PoseServer;
class InputServer;
class SharedState;

extern "C" __declspec(dllexport)
void* HmdDriverFactory(const char* pInterfaceName, int* pReturnCode);

class YunaDriverProvider : public vr::IServerTrackedDeviceProvider
{
public:
    vr::EVRInitError   Init(vr::IVRDriverContext* pDriverContext) override;
    void               Cleanup() override;
    const char* const* GetInterfaceVersions() override;
    void               RunFrame() override;
    bool               ShouldBlockStandbyMode() override;
    void               EnterStandby() override;
    void               LeaveStandby() override;

private:
    std::unique_ptr<SharedState>     m_state;
    std::unique_ptr<PoseServer>      m_poseServer;
    std::unique_ptr<InputServer>     m_inputServer;
    std::shared_ptr<YunaHMD>         m_hmd;
    std::shared_ptr<YunaController>  m_ctrlLeft;
    std::shared_ptr<YunaController>  m_ctrlRight;
};
