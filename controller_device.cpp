// Project YUNA Link - controller_device.cpp

#include "driver_main.h"
#include "controller_device.h"
#include <cstring>

YunaController::YunaController(vr::ETrackedControllerRole role, PoseServer* poseServer)
    : m_role(role), m_poseServer(poseServer)
{
    m_serial = IsLeft() ? "YUNA_CTRL_LEFT_001" : "YUNA_CTRL_RIGHT_001";
    InitDefaultPose();
}

void YunaController::InitDefaultPose()
{
    m_defaultPose = {};
    m_defaultPose.poseIsValid                = true;
    m_defaultPose.result                     = vr::TrackingResult_Running_OK;
    m_defaultPose.deviceIsConnected          = true;
    m_defaultPose.vecPosition[0]             = IsLeft() ? -0.25 : 0.25;
    m_defaultPose.vecPosition[1]             = 1.1;
    m_defaultPose.vecPosition[2]             = -0.1;
    m_defaultPose.qRotation.w                = 1.0;
    m_defaultPose.qWorldFromDriverRotation.w = 1.0;
    m_defaultPose.qDriverFromHeadRotation.w  = 1.0;
}

vr::EVRInitError YunaController::Activate(uint32_t unObjectId)
{
    m_deviceId = unObjectId;
    DriverLog("[YUNA CTRL] Activate %s id=%u\n",
              IsLeft() ? "LEFT" : "RIGHT", unObjectId);

    vr::CVRPropertyHelpers* props = vr::VRProperties();
    vr::PropertyContainerHandle_t c =
        props->TrackedDeviceToPropertyContainer(m_deviceId);

    props->SetStringProperty(c, vr::Prop_ManufacturerName_String,   "YUNA Project");
    props->SetStringProperty(c, vr::Prop_ModelNumber_String,        "YUNA Controller v0.1");
    props->SetStringProperty(c, vr::Prop_SerialNumber_String,       m_serial.c_str());
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String, "YUNA");
    props->SetInt32Property (c, vr::Prop_ControllerRoleHint_Int32,  m_role);
    props->SetStringProperty(c, vr::Prop_InputProfilePath_String,
        "{yuna}/input/yuna_controller_profile.json");

    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/trigger/click", &m_triggerClick);
    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/grip/click",    &m_gripClick);
    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/system/click",  &m_systemClick);
    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/a/click",       &m_aClick);
    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/b/click",       &m_bClick);
    vr::VRDriverInput()->CreateScalarComponent(c, "/input/trigger/value",
        &m_triggerValue, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedOneSided);
    vr::VRDriverInput()->CreateScalarComponent(c, "/input/joystick/x",
        &m_joystickX, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    vr::VRDriverInput()->CreateScalarComponent(c, "/input/joystick/y",
        &m_joystickY, vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);

    return vr::VRInitError_None;
}

void YunaController::Deactivate()
{
    DriverLog("[YUNA CTRL] Deactivate %s\n", IsLeft() ? "LEFT" : "RIGHT");
    m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
}

void YunaController::EnterStandby() {}

void* YunaController::GetComponent(const char*) { return nullptr; }

void YunaController::DebugRequest(const char*, char* pchResponseBuffer,
                                   uint32_t unResponseBufferSize)
{
    if (unResponseBufferSize > 0) pchResponseBuffer[0] = '\0';
}

vr::DriverPose_t YunaController::GetPose()
{
    if (m_poseServer)
    {
        if (IsLeft()  && m_poseServer->HasLeftControllerPose())
            return m_poseServer->GetLeftControllerPose();
        if (!IsLeft() && m_poseServer->HasRightControllerPose())
            return m_poseServer->GetRightControllerPose();
    }
    return m_defaultPose;
}

void YunaController::RunFrame()
{
    if (m_deviceId == vr::k_unTrackedDeviceIndexInvalid) return;

    bool has = IsLeft()
        ? (m_poseServer && m_poseServer->HasLeftControllerPose())
        : (m_poseServer && m_poseServer->HasRightControllerPose());

    if (has)
        vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
            m_deviceId, GetPose(), sizeof(vr::DriverPose_t));

    if (!m_poseServer) return;

    ControllerInput in = IsLeft()
        ? m_poseServer->GetLeftInput()
        : m_poseServer->GetRightInput();

    vr::VRDriverInput()->UpdateBooleanComponent(m_triggerClick, in.triggerClick, 0);
    vr::VRDriverInput()->UpdateBooleanComponent(m_gripClick,    in.gripClick,    0);
    vr::VRDriverInput()->UpdateBooleanComponent(m_aClick,       in.aClick,       0);
    vr::VRDriverInput()->UpdateBooleanComponent(m_bClick,       in.bClick,       0);
    vr::VRDriverInput()->UpdateScalarComponent (m_triggerValue, in.triggerValue, 0);
    vr::VRDriverInput()->UpdateScalarComponent (m_joystickX,    in.joystickX,    0);
    vr::VRDriverInput()->UpdateScalarComponent (m_joystickY,    in.joystickY,    0);
}
