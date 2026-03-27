// Project YUNA Link - hmd_device.cpp

#include "driver_main.h"
#include "hmd_device.h"
#include <cstring>

YunaHMD::YunaHMD(PoseServer* poseServer)
    : m_poseServer(poseServer)
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
    m_defaultPose.vecPosition[0]             = 0.0;
    m_defaultPose.vecPosition[1]             = 1.6;
    m_defaultPose.vecPosition[2]             = 0.0;
    m_defaultPose.qRotation.w                = 1.0;
    m_defaultPose.qWorldFromDriverRotation.w = 1.0;
    m_defaultPose.qDriverFromHeadRotation.w  = 1.0;
}

// ---------------------------------------------------------------------------
// ITrackedDeviceServerDriver
// ---------------------------------------------------------------------------
vr::EVRInitError YunaHMD::Activate(uint32_t unObjectId)
{
    m_deviceId = unObjectId;
    DriverLog("[YUNA HMD] Activate id=%u\n", unObjectId);

    // Use explicit local variable to avoid any ambiguity with
    // IVRSettings::SetBool vs CVRPropertyHelpers::SetBoolProperty
    vr::CVRPropertyHelpers* props = vr::VRProperties();
    vr::PropertyContainerHandle_t c =
        props->TrackedDeviceToPropertyContainer(m_deviceId);

    props->SetStringProperty(c, vr::Prop_ManufacturerName_String,         "YUNA Project");
    props->SetStringProperty(c, vr::Prop_ModelNumber_String,              "YUNA HMD v0.1");
    props->SetStringProperty(c, vr::Prop_SerialNumber_String,             GetSerialNumber());
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String,       "YUNA");
    props->SetFloatProperty (c, vr::Prop_UserIpdMeters_Float,             0.063f);
    props->SetFloatProperty (c, vr::Prop_DisplayFrequency_Float,          90.f);
    props->SetFloatProperty (c, vr::Prop_SecondsFromVsyncToPhotons_Float, 0.011f);
    props->SetBoolProperty  (c, vr::Prop_IsOnDesktop_Bool,                false);
    props->SetBoolProperty  (c, vr::Prop_HasDisplayComponent_Bool,        true);
    props->SetBoolProperty  (c, vr::Prop_HasVirtualDisplayComponent_Bool, true);

    // Universe ID: fixed non-zero value to resolve "universe id is invalid"
    props->SetUint64Property(c, vr::Prop_CurrentUniverseId_Uint64,  2);
    props->SetUint64Property(c, vr::Prop_PreviousUniverseId_Uint64, 2);

    // Chaperone: 2m x 2m standing play area centred at origin
    // jsonid must be "chaperone_info" not "vrpathreg"
    static const char kChaperoneJson[] =
        "{"
        "\"jsonid\":\"chaperone_info\","
        "\"universe_id\":2,"
        "\"standing\":{"
            "\"translation\":[0.0,0.0,0.0],"
            "\"yaw\":0.0,"
            "\"collision_bounds\":["
                "[[-1.0,0.0,-1.0],[-1.0,2.5,-1.0],[-1.0,2.5,1.0],[-1.0,0.0,1.0]],"
                "[[-1.0,0.0, 1.0],[-1.0,2.5, 1.0],[ 1.0,2.5,1.0],[ 1.0,0.0,1.0]],"
                "[[ 1.0,0.0, 1.0],[ 1.0,2.5, 1.0],[ 1.0,2.5,-1.0],[ 1.0,0.0,-1.0]],"
                "[[ 1.0,0.0,-1.0],[ 1.0,2.5,-1.0],[-1.0,2.5,-1.0],[-1.0,0.0,-1.0]]"
            "],"
            "\"play_area\":[2.0,2.0]"
        "}"
        "}";

    props->SetStringProperty(c, vr::Prop_DriverProvidedChaperoneJson_String,
                             kChaperoneJson);
    props->SetBoolProperty  (c, vr::Prop_DriverProvidedChaperoneVisibility_Bool,
                             true);

    // Start preview window - non-blocking, returns immediately
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

void* YunaHMD::GetComponent(const char* pchNameAndVersion)
{
    if (strcmp(pchNameAndVersion, vr::IVRDisplayComponent_Version) == 0)
        return static_cast<vr::IVRDisplayComponent*>(this);
    if (strcmp(pchNameAndVersion, vr::IVRVirtualDisplay_Version) == 0)
        return static_cast<vr::IVRVirtualDisplay*>(this);
    return nullptr;
}

void YunaHMD::DebugRequest(const char*, char* buf, uint32_t sz)
{
    if (sz > 0) buf[0] = '\0';
}

vr::DriverPose_t YunaHMD::GetPose()
{
    if (m_poseServer && m_poseServer->HasHMDPose())
        return m_poseServer->GetHMDPose();
    return m_defaultPose;
}

void YunaHMD::RunFrame()
{
    if (m_deviceId == vr::k_unTrackedDeviceIndexInvalid) return;
    if (m_poseServer && m_poseServer->HasHMDPose())
        vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
            m_deviceId, GetPose(), sizeof(vr::DriverPose_t));
}

// ---------------------------------------------------------------------------
// IVRDisplayComponent_003
// ---------------------------------------------------------------------------
void YunaHMD::GetWindowBounds(int32_t* pnX, int32_t* pnY,
                               uint32_t* pnWidth, uint32_t* pnHeight)
{
    *pnX=0; *pnY=0; *pnWidth=EYE_W*2; *pnHeight=EYE_H;
}

bool YunaHMD::IsDisplayOnDesktop()  { return false; }
bool YunaHMD::IsDisplayRealDisplay(){ return false; }

void YunaHMD::GetRecommendedRenderTargetSize(uint32_t* pnW, uint32_t* pnH)
{
    *pnW=EYE_W; *pnH=EYE_H;
}

void YunaHMD::GetEyeOutputViewport(vr::EVREye eEye,
    uint32_t* pnX, uint32_t* pnY, uint32_t* pnW, uint32_t* pnH)
{
    *pnY=0; *pnW=EYE_W; *pnH=EYE_H;
    *pnX = (eEye == vr::Eye_Left) ? 0 : EYE_W;
}

void YunaHMD::GetProjectionRaw(vr::EVREye,
    float* pfL, float* pfR, float* pfT, float* pfB)
{
    *pfL=-1.f; *pfR=1.f; *pfT=-1.f; *pfB=1.f;
}

vr::DistortionCoordinates_t YunaHMD::ComputeDistortion(vr::EVREye, float u, float v)
{
    vr::DistortionCoordinates_t d{};
    d.rfRed[0]=d.rfGreen[0]=d.rfBlue[0]=u;
    d.rfRed[1]=d.rfGreen[1]=d.rfBlue[1]=v;
    return d;
}

bool YunaHMD::ComputeInverseDistortion(vr::HmdVector2_t* pR,
    vr::EVREye, uint32_t, float u, float v)
{
    if (pR){ pR->v[0]=u; pR->v[1]=v; } return true;
}

// ---------------------------------------------------------------------------
// IVRVirtualDisplay_002
// ---------------------------------------------------------------------------
void YunaHMD::Present(const vr::PresentInfo_t* pInfo, uint32_t)
{
    if (!pInfo) return;
    static uint64_t s_count = 0;
    if (++s_count == 1)
        DriverLog("[YUNA HMD] Present() first call, handle=0x%llX\n",
                  (unsigned long long)pInfo->backbufferTextureHandle);
    if (m_dispWindow)
        m_dispWindow->Present(pInfo->backbufferTextureHandle);
}

void YunaHMD::WaitForPresent()
{
    if (m_dispWindow) m_dispWindow->WaitForPresent();
}

bool YunaHMD::GetTimeSinceLastVsync(float* pfSec, uint64_t* pFrame)
{
    if (m_dispWindow)
        return m_dispWindow->GetTimeSinceLastVsync(pfSec, pFrame);
    return false;
}
