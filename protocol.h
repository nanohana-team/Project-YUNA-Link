#pragma once
// Project YUNA Link - protocol.h
// Wire protocol shared between driver (C++) and Python client.
// ALL changes here must be reflected in apps/yuna_link.py

#include <cstdint>

#pragma pack(push, 1)

// ---------------------------------------------------------------------------
// HMD pose packet  (pipe: YunaLinkPose, type=0x01)
// ---------------------------------------------------------------------------
//  Header: type(u8) + length(u16) = 3 bytes
//  Body  : HmdPosePacket           = 57 bytes
struct HmdPosePacket
{
    uint8_t device;          // 0 = HMD
    double  px, py, pz;
    double  qw, qx, qy, qz;
};

// ---------------------------------------------------------------------------
// FramePacket  (pipe: YunaLinkPose, type=0x10)
// One packet per frame, carries BOTH controller poses AND inputs.
//
// struct layout (little-endian):
//   frameId    : uint64   8 bytes
//   timestamp  : double   8 bytes
//   --- left controller pose (29 bytes) ---
//   lPx,lPy,lPz: float   12 bytes
//   lQx,lQy,lQz,lQw: float 16 bytes
//   lTrack     : uint8    1 byte   (0=invalid 1=valid)
//   lConn      : uint8    1 byte
//   --- right controller pose (29 bytes) ---
//   rPx,rPy,rPz: float   12 bytes
//   rQx,rQy,rQz,rQw: float 16 bytes
//   rTrack     : uint8    1 byte
//   rConn      : uint8    1 byte
//   --- left input (9 bytes) ---
//   lAButton   : uint8    1 byte
//   lStickX    : float    4 bytes
//   lStickY    : float    4 bytes
//   --- right input (9 bytes) ---
//   rAButton   : uint8    1 byte
//   rStickX    : float    4 bytes
//   rStickY    : float    4 bytes
//   --- global (1 byte) ---
//   startButton: uint8    1 byte
//
// Total body = 8+8 + 29+29 + 9+9 + 1 = 93 bytes
// ---------------------------------------------------------------------------
struct ControllerPoseData
{
    float   px, py, pz;
    float   qx, qy, qz, qw;
    uint8_t trackingValid;   // 1 = valid
    uint8_t connected;       // 1 = connected
};

struct ControllerInputData
{
    uint8_t aButton;
    float   stickX;
    float   stickY;
};

struct FramePacket
{
    uint64_t          frameId;
    double            timestamp;
    ControllerPoseData leftPose;
    ControllerPoseData rightPose;
    ControllerInputData leftInput;
    ControllerInputData rightInput;
    uint8_t           startButton;
};

// ---------------------------------------------------------------------------
// Packet header  (3 bytes, type + length of body)
// ---------------------------------------------------------------------------
struct PacketHeader
{
    uint8_t  type;
    uint16_t length;
};

enum PacketType : uint8_t
{
    PKT_HMD_POSE  = 0x01,   // HmdPosePacket body
    PKT_FRAME     = 0x10,   // FramePacket body
};

#pragma pack(pop)

static_assert(sizeof(HmdPosePacket)        == 57,  "HmdPosePacket size mismatch");
static_assert(sizeof(ControllerPoseData)   == 30,  "ControllerPoseData size mismatch");
static_assert(sizeof(ControllerInputData)  ==  9,  "ControllerInputData size mismatch");
static_assert(sizeof(FramePacket)          == 93,  "FramePacket size mismatch");
