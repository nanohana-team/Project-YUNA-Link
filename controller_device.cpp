// Project YUNA Link - controller_device.cpp

#include "driver_main.h"
#include "controller_device.h"
#include <cstring>

YunaController::YunaController(vr::ETrackedControllerRole role, SharedState* state)
    : m_role(role), m_state(state)
{
    m_serial = IsLeft() ? "YUNA_CTRL_LEFT_001" : "YUNA_CTRL_RIGHT_001";
}

vr::EVRInitError YunaController::Activate(uint32_t unObjectId)
{
    m_deviceId = unObjectId;
    DriverLog("[YUNA CTRL] Activate %s id=%u\n", IsLeft()?"LEFT":"RIGHT", unObjectId);

    vr::CVRPropertyHelpers* props = vr::VRProperties();
    vr::PropertyContainerHandle_t c = props->TrackedDeviceToPropertyContainer(m_deviceId);

    props->SetStringProperty(c, vr::Prop_ManufacturerName_String,   "YUNA Project");
    props->SetStringProperty(c, vr::Prop_ModelNumber_String,        "YUNA Controller v1.0");
    props->SetStringProperty(c, vr::Prop_SerialNumber_String,       m_serial.c_str());
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String, "YUNA");
    props->SetStringProperty(c, vr::Prop_ControllerType_String,     "yuna_controller");
    props->SetInt32Property (c, vr::Prop_ControllerRoleHint_Int32,  m_role);
    props->SetStringProperty(c, vr::Prop_InputProfilePath_String,
        "{yuna}/input/yuna_controller_profile.json");

    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/start/click",  &m_startClick);
    vr::VRDriverInput()->CreateBooleanComponent(c, "/input/a/click",      &m_aClick);
    vr::VRDriverInput()->CreateScalarComponent (c, "/input/thumbstick/x", &m_thumbstickX,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    vr::VRDriverInput()->CreateScalarComponent (c, "/input/thumbstick/y", &m_thumbstickY,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);

    return vr::VRInitError_None;
}

void YunaController::Deactivate()
{
    DriverLog("[YUNA CTRL] Deactivate %s\n", IsLeft()?"LEFT":"RIGHT");
    m_deviceId = vr::k_unTrackedDeviceIndexInvalid;
}

void YunaController::EnterStandby() {}
void* YunaController::GetComponent(const char*) { return nullptr; }
void YunaController::DebugRequest(const char*, char* buf, uint32_t sz)
{ if(sz>0) buf[0]='\0'; }

vr::DriverPose_t YunaController::GetPose()
{
    auto cs = IsLeft() ? m_state->getLeft() : m_state->getRight();
    return cs.pose;
}

void YunaController::RunFrame()
{
    if (m_deviceId == vr::k_unTrackedDeviceIndexInvalid) return;

    // Failsafe: mark tracking invalid if no packet for 250ms
    m_state->applyFailsafe();

    vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
        m_deviceId, GetPose(), sizeof(vr::DriverPose_t));

    // Reflect input state
    GlobalInputState gs = m_state->getInput();

    vr::VRDriverInput()->UpdateBooleanComponent(m_startClick,  gs.startButton,      0.0);
    if (IsLeft())
    {
        vr::VRDriverInput()->UpdateBooleanComponent(m_aClick,      false,              0.0);
        vr::VRDriverInput()->UpdateScalarComponent (m_thumbstickX, gs.left.stickX,    0.0);
        vr::VRDriverInput()->UpdateScalarComponent (m_thumbstickY, gs.left.stickY,    0.0);
    }
    else
    {
        vr::VRDriverInput()->UpdateBooleanComponent(m_aClick,      gs.right.aButton,  0.0);
        vr::VRDriverInput()->UpdateScalarComponent (m_thumbstickX, gs.right.stickX,   0.0);
        vr::VRDriverInput()->UpdateScalarComponent (m_thumbstickY, gs.right.stickY,   0.0);
    }
}
