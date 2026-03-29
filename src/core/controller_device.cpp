// Project YUNA Link - controller_device.cpp
// Presents as Oculus Touch.
// Key fix: SteamVR oculus_touch binding expects
//   /input/joystick  (not /input/thumbstick)
//   /input/grip      (not /input/squeeze)

#include "driver_main.h"
#include "controller_device.h"
#include <cmath>

static vr::EVRInputError LogCreate(const char* side, const char* path,
                                   vr::EVRInputError err,
                                   vr::VRInputComponentHandle_t handle)
{
    if (err != vr::VRInputError_None)
        DriverLog("[YUNA CTRL][%s] Create FAILED path=%s err=%d\n", side, path, (int)err);
    else
        DriverLog("[YUNA CTRL][%s] Create OK   path=%s handle=%llu\n",
                  side, path, (unsigned long long)handle);
    return err;
}

#define CREATE_BOOL(path, handle) \
    LogCreate(side, (path), inp->CreateBooleanComponent(c, (path), &(handle)), (handle))
#define CREATE_SCALAR(path, handle, stype, sunit) \
    LogCreate(side, (path), inp->CreateScalarComponent(c, (path), &(handle), (stype), (sunit)), (handle))

YunaController::YunaController(vr::ETrackedControllerRole role, SharedState* state)
    : m_role(role), m_state(state)
{
    m_serial = IsLeft() ? "YUNA_CTRL_LEFT_020" : "YUNA_CTRL_RIGHT_020";
}

vr::EVRInitError YunaController::Activate(uint32_t unObjectId)
{
    m_deviceId = unObjectId;
    const char* side = IsLeft() ? "LEFT" : "RIGHT";
    DriverLog("[YUNA CTRL] Activate %s id=%u\n", side, unObjectId);

    vr::CVRPropertyHelpers* props = vr::VRProperties();
    vr::PropertyContainerHandle_t c = props->TrackedDeviceToPropertyContainer(m_deviceId);

    props->SetStringProperty(c, vr::Prop_ManufacturerName_String,   "Oculus");
    props->SetStringProperty(c, vr::Prop_ModelNumber_String,
        IsLeft() ? "Oculus Rift CV1 (Left Controller)"
                 : "Oculus Rift CV1 (Right Controller)");
    props->SetStringProperty(c, vr::Prop_SerialNumber_String,       m_serial.c_str());
    props->SetStringProperty(c, vr::Prop_TrackingSystemName_String, "oculus");
    props->SetStringProperty(c, vr::Prop_ControllerType_String,     "oculus_touch");
    props->SetInt32Property (c, vr::Prop_ControllerRoleHint_Int32,  m_role);

    const char* profilePath = IsLeft()
        ? "{yuna}/input/oculus_touch_profile_left.json"
        : "{yuna}/input/oculus_touch_profile_right.json";
    props->SetStringProperty(c, vr::Prop_InputProfilePath_String, profilePath);
    DriverLog("[YUNA CTRL][%s] InputProfilePath = %s\n", side, profilePath);

    props->SetStringProperty(c, vr::Prop_RenderModelName_String,
        IsLeft() ? "oculus_cv1_controller_left"
                 : "oculus_cv1_controller_right");

    auto* inp = vr::VRDriverInput();

    // System / AppMenu
    CREATE_BOOL("/input/system/click",           m_system);
    CREATE_BOOL("/input/application_menu/click", m_appMenu);

    // Trigger
    CREATE_SCALAR("/input/trigger/value", m_triggerVal,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedOneSided);
    CREATE_BOOL("/input/trigger/click", m_triggerClk);
    CREATE_BOOL("/input/trigger/touch", m_triggerTouch);

    // Grip  -> was /input/squeeze, changed to /input/grip to match oculus_touch binding
    CREATE_SCALAR("/input/grip/value", m_gripVal,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedOneSided);
    CREATE_BOOL("/input/grip/click", m_gripClk);

    // Joystick  -> was /input/thumbstick, changed to /input/joystick to match oculus_touch binding
    CREATE_SCALAR("/input/joystick/x", m_joyX,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    CREATE_SCALAR("/input/joystick/y", m_joyY,
        vr::VRScalarType_Absolute, vr::VRScalarUnits_NormalizedTwoSided);
    CREATE_BOOL("/input/joystick/click", m_joyClick);
    CREATE_BOOL("/input/joystick/touch", m_joyTouch);

    if (IsLeft())
    {
        CREATE_BOOL("/input/x/click", m_xClick);
        CREATE_BOOL("/input/y/click", m_yClick);
    }
    else
    {
        CREATE_BOOL("/input/a/click", m_aClick);
        CREATE_BOOL("/input/b/click", m_bClick);
    }

    DriverLog("[YUNA CTRL][%s] Activate complete\n", side);
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

    m_state->applyFailsafe();
    vr::VRServerDriverHost()->TrackedDevicePoseUpdated(
        m_deviceId, GetPose(), sizeof(vr::DriverPose_t));

    GlobalInputState gs = m_state->getInput();
    auto* inp = vr::VRDriverInput();

    inp->UpdateBooleanComponent(m_system,  gs.startButton, 0.0);
    inp->UpdateBooleanComponent(m_appMenu, gs.menuButton,  0.0);

    if (IsLeft())
    {
        // Trigger
        inp->UpdateScalarComponent (m_triggerVal,   gs.left.triggerValue,          0.0);
        inp->UpdateBooleanComponent(m_triggerClk,   gs.left.triggerValue >= 0.7f,  0.0);
        inp->UpdateBooleanComponent(m_triggerTouch, gs.left.triggerValue >  0.01f, 0.0);

        // Grip
        inp->UpdateScalarComponent (m_gripVal, gs.left.gripValue,           0.0);
        inp->UpdateBooleanComponent(m_gripClk, gs.left.gripValue >= 0.7f,   0.0);

        // Joystick
        float lx = gs.left.stickX, ly = gs.left.stickY;
        float lm2 = lx*lx + ly*ly;
        vr::EVRInputError ex = inp->UpdateScalarComponent(m_joyX, lx, 0.0);
        vr::EVRInputError ey = inp->UpdateScalarComponent(m_joyY, ly, 0.0);
        inp->UpdateBooleanComponent(m_joyTouch, lm2 > 0.05f*0.05f, 0.0);
        inp->UpdateBooleanComponent(m_joyClick, lm2 > 0.70f*0.70f, 0.0);

        static bool s_logL = false;
        if (!s_logL && (ex || ey))
        { DriverLog("[JOY][L] FAILED ex=%d ey=%d\n",(int)ex,(int)ey); s_logL=true; }
        if (fabsf(lx)>0.01f || fabsf(ly)>0.01f)
            DriverLog("[JOY][L] x=%.3f y=%.3f touch=%d ex=%d ey=%d\n",
                lx,ly,lm2>0.05f*0.05f,(int)ex,(int)ey);

        // X / Y
        inp->UpdateBooleanComponent(m_xClick, gs.left.xButton, 0.0);
        inp->UpdateBooleanComponent(m_yClick, gs.left.yButton, 0.0);
    }
    else
    {
        // Trigger
        inp->UpdateScalarComponent (m_triggerVal,   gs.right.triggerValue,          0.0);
        inp->UpdateBooleanComponent(m_triggerClk,   gs.right.triggerValue >= 0.7f,  0.0);
        inp->UpdateBooleanComponent(m_triggerTouch, gs.right.triggerValue >  0.01f, 0.0);

        // Grip
        inp->UpdateScalarComponent (m_gripVal, gs.right.gripValue,           0.0);
        inp->UpdateBooleanComponent(m_gripClk, gs.right.gripValue >= 0.7f,   0.0);

        // Joystick
        float rx = gs.right.stickX, ry = gs.right.stickY;
        float rm2 = rx*rx + ry*ry;
        vr::EVRInputError ex = inp->UpdateScalarComponent(m_joyX, rx, 0.0);
        vr::EVRInputError ey = inp->UpdateScalarComponent(m_joyY, ry, 0.0);
        inp->UpdateBooleanComponent(m_joyTouch, rm2 > 0.05f*0.05f, 0.0);
        inp->UpdateBooleanComponent(m_joyClick, rm2 > 0.70f*0.70f, 0.0);

        static bool s_logR = false;
        if (!s_logR && (ex || ey))
        { DriverLog("[JOY][R] FAILED ex=%d ey=%d\n",(int)ex,(int)ey); s_logR=true; }
        if (fabsf(rx)>0.01f || fabsf(ry)>0.01f)
            DriverLog("[JOY][R] x=%.3f y=%.3f touch=%d ex=%d ey=%d\n",
                rx,ry,rm2>0.05f*0.05f,(int)ex,(int)ey);

        // A / B
        inp->UpdateBooleanComponent(m_aClick, gs.right.aButton, 0.0);
        inp->UpdateBooleanComponent(m_bClick, gs.right.bButton, 0.0);
    }
}
