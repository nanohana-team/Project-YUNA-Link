#pragma once
// Project YUNA Link - protocol.h
// Wire protocol: C++ <-> Python  (ALL changes must match apps/yuna_link.py)

#include <cstdint>

#pragma pack(push, 1)

// ---------------------------------------------------------------------------
// HmdPosePacket  (type=0x01, body=57 bytes)
// ---------------------------------------------------------------------------
struct HmdPosePacket
{
    uint8_t device;          // always 0
    double  px, py, pz;
    double  qw, qx, qy, qz;
};

// ---------------------------------------------------------------------------
// ControllerPoseData  (30 bytes per hand)
// ---------------------------------------------------------------------------
struct ControllerPoseData
{
    float   px, py, pz;
    float   qx, qy, qz, qw;
    uint8_t trackingValid;
    uint8_t connected;
};

// ---------------------------------------------------------------------------
// ControllerInputData  (20 bytes per hand)
//
//   aButton     u8   -- right hand only (spec); registered on both
//   bButton     u8   -- right hand only
//   xButton     u8   -- left hand only
//   yButton     u8   -- left hand only
//   triggerValue f32 -- 0.0 ~ 1.0  RTRIGGER / LTRIGGER
//   gripValue    f32 -- 0.0 ~ 1.0  RGRIP    / LGRIP
//   stickX       f32 -- -1.0 ~ 1.0
//   stickY       f32 -- -1.0 ~ 1.0
// ---------------------------------------------------------------------------
struct ControllerInputData
{
    uint8_t aButton;
    uint8_t bButton;
    uint8_t xButton;
    uint8_t yButton;
    float   triggerValue;
    float   gripValue;
    float   stickX;
    float   stickY;
};

// ---------------------------------------------------------------------------
// FramePacket  (type=0x10, body=118 bytes)
//
//  frameId      u64   8
//  timestamp    f64   8
//  leftPose           30
//  rightPose          30
//  leftInput          20
//  rightInput         20
//  startButton  u8    1
//  menuButton   u8    1
//  ---------------------
//  total              118
// ---------------------------------------------------------------------------
struct FramePacket
{
    uint64_t           frameId;
    double             timestamp;
    ControllerPoseData leftPose;
    ControllerPoseData rightPose;
    ControllerInputData leftInput;
    ControllerInputData rightInput;
    uint8_t            startButton;
    uint8_t            menuButton;
};

// ---------------------------------------------------------------------------
// Packet header (3 bytes)
// ---------------------------------------------------------------------------
struct PacketHeader
{
    uint8_t  type;
    uint16_t length;
};

enum PacketType : uint8_t
{
    PKT_HMD_POSE = 0x01,
    PKT_FRAME    = 0x10,
};

#pragma pack(pop)

static_assert(sizeof(HmdPosePacket)        == 57,  "HmdPosePacket size");
static_assert(sizeof(ControllerPoseData)   == 30,  "ControllerPoseData size");
static_assert(sizeof(ControllerInputData)  == 20,  "ControllerInputData size");
static_assert(sizeof(FramePacket)          == 118, "FramePacket size");
