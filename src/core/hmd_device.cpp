// Project YUNA Link - hmd_device.cpp

#include "driver_main.h"
#include "hmd_device.h"
#include <cstring>

YunaHMD::YunaHMD(SharedState* state)
    : m_state(state)
    , m_dispWindow(std::make_unique<DisplayWindow>())
{
    InitDefaultPose();
}

void YunaHMD::InitDefaultPose()
{
    m_defaultPose = {};
    m_defaultPose.poseIsValid                = true;
    m_defaultPose.result                     = vr::TrackingResult_Running_OK;
    m_defaultPose.deviceIsConnected          = true;
    m_defaultPose.vecPosition[1]             = 1.6;
    m_defaultPose.qRotation.w                = 1.0;
    m_defaultPose.qWorldFromDriverRotation.w = 1.0;
    m_defaultPose.qDriverFromHeadRotation.w  = 1.0;
}

vr::EVRInitError YunaHMD::Activate(uint32_t unObjectId)
{
    m_deviceId = unObjectId;
    DriverLog("[YUNA HMD] Activate id=%u\n", unObjectId);

    vr::CVRPropertyHelpers* props = vr::VRProperties();
    vr::PropertyContainerHandle_t c = props->TrackedDeviceToPropertyContainer(m_deviceId);

    props->SetStringProperty(c, vr::Prop_ManufacturerName_String,         "YUNA Project");
    props->SetStringProperty(c, vr::Prop_ModelNumber_String,              "YUNA HMD v1.0");
    props->SetStringProperty(c, vr::Prop_SerialNumber_String,             GetSerialNumber());
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String,       "YUNA");
    props->SetFloatProperty  (c, vr::Prop_UserIpdMeters_Float,            0.063f);
    props->SetFloatProperty  (c, vr::Prop_DisplayFrequency_Float,         90.f);
    props->SetFloatProperty  (c, vr::Prop_SecondsFromVsyncToPhotons_Float,0.011f);
    props->SetBoolProperty   (c, vr::Prop_IsOnDesktop_Bool,               false);
    props->SetBoolProperty   (c, vr::Prop_HasDisplayComponent_Bool,       true);
    props->SetBoolProperty   (c, vr::Prop_HasVirtualDisplayComponent_Bool,true);
    props->SetUint64Property (c, vr::Prop_CurrentUniverseId_Uint64,       2);
    props->SetUint64Property (c, vr::Prop_PreviousUniverseId_Uint64,      2);

    static const char kChaperone[] =
        "{\"jsonid\":\"chaperone_info\",\"universe_id\":2,\"standing\":{"
        "\"translation\":[0,0,0],\"yaw\":0,"
        "\"collision_bounds\":["
        "[[-1,0,-1],[-1,2.5,-1],[-1,2.5,1],[-1,0,1]],"
        "[[-1,0,1],[-1,2.5,1],[1,2.5,1],[1,0,1]],"
        "[[1,0,1],[1,2.5,1],[1,2.5,-1],[1,0,-1]],"
        "[[1,0,-1],[1,2.5,-1],[-1,2.5,-1],[-1,0,-1]]],"
        "\"play_area\":[2,2]}}";

    props->SetStringProperty(c, vr::Prop_DriverProvidedChaperoneJson_String, kChaperone);
    props->SetBoolProperty  (c, vr::Prop_DriverProvidedChaperoneVisibility_Bool, true);

    m_dispWindow->Start(EYE_W, EYE_H);
    return vr::VRInitError_None;
}

void YunaHMD::Deactivate()
{
    DriverLog("[YUNA HMD] Deactivate\n");
    if (m_dispWindow) m_dispWindow->Stop();
    m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
}

void YunaHMD::EnterStandby() {}

void* YunaHMD::GetComponent(const char* name)
{
    if (strcmp(name, vr::IVRDisplayComponent_Version) == 0)
        return static_cast<vr::IVRDisplayComponent*>(this);
    if (strcmp(name, vr::IVRVirtualDisplay_Version) == 0)
        return static_cast<vr::IVRVirtualDisplay*>(this);
    return nullptr;
}

void YunaHMD::DebugRequest(const char*, char* buf, uint32_t sz)
{ if(sz>0) buf[0]='\0'; }

vr::DriverPose_t YunaHMD::GetPose()
{
    if (m_state->hasHmdPose()) return m_state->getHmdPose();
    return m_defaultPose;
}

void YunaHMD::RunFrame()
{
    if (m_deviceId == vr::k_unTrackedDeviceIndexInvalid) return;
    m_state->applyFailsafe();
    vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
        m_deviceId, GetPose(), sizeof(vr::DriverPose_t));
}

// IVRDisplayComponent
void YunaHMD::GetWindowBounds(int32_t* x, int32_t* y, uint32_t* w, uint32_t* h)
{
    *x = 0;
    *y = 0;
    *w = EYE_W;
    *h = EYE_H;
}

bool YunaHMD::IsDisplayOnDesktop()   { return false; }
bool YunaHMD::IsDisplayRealDisplay() { return false; }

void YunaHMD::GetRecommendedRenderTargetSize(uint32_t* w, uint32_t* h)
{ *w=EYE_W; *h=EYE_H; }

void YunaHMD::GetEyeOutputViewport(vr::EVREye,
    uint32_t* x, uint32_t* y, uint32_t* w, uint32_t* h)
{
    *x = 0;
    *y = 0;
    *w = EYE_W;
    *h = EYE_H;
}

void YunaHMD::GetProjectionRaw(vr::EVREye, float* l, float* r, float* t, float* b)
{ *l=-1.f; *r=1.f; *t=-1.f; *b=1.f; }

vr::DistortionCoordinates_t YunaHMD::ComputeDistortion(vr::EVREye, float u, float v)
{
    vr::DistortionCoordinates_t d{};
    d.rfRed[0]=d.rfGreen[0]=d.rfBlue[0]=u;
    d.rfRed[1]=d.rfGreen[1]=d.rfBlue[1]=v;
    return d;
}

bool YunaHMD::ComputeInverseDistortion(vr::HmdVector2_t* r,
    vr::EVREye, uint32_t, float u, float v)
{ if(r){r->v[0]=u;r->v[1]=v;} return true; }

// IVRVirtualDisplay
void YunaHMD::Present(const vr::PresentInfo_t* info, uint32_t)
{
    if (!info) return;
    static uint64_t cnt=0;
    if (++cnt==1)
        DriverLog("[YUNA HMD] Present() first call handle=0x%llX\n",
                  (unsigned long long)info->backbufferTextureHandle);
    if (m_dispWindow) m_dispWindow->Present(info->backbufferTextureHandle);
}

void YunaHMD::WaitForPresent()
{ if(m_dispWindow) m_dispWindow->WaitForPresent(); }

bool YunaHMD::GetTimeSinceLastVsync(float* s, uint64_t* f)
{ return m_dispWindow ? m_dispWindow->GetTimeSinceLastVsync(s,f) : false; }
