#pragma once
// Project YUNA Link - pose_server.h
// Receives HmdPosePacket (0x01) and FramePacket (0x10) on YunaLinkPose.

#include "shared_state.h"
#include <windows.h>
#include <thread>
#include <atomic>
#include <vector>

#define YUNA_PIPE_NAME R"(\\.\pipe\YunaLinkPose)"

class PoseServer
{
public:
    explicit PoseServer(SharedState* state);
    ~PoseServer();

    void Start();
    void Stop();

private:
    void ServerThread();
    bool ReadExact(HANDLE hPipe, void* buf, DWORD bytes);
    void DispatchPacket(uint8_t type, const std::vector<uint8_t>& body);

    SharedState*       m_state;
    std::thread        m_thread;
    std::atomic<bool>  m_running{ false };
    HANDLE             m_stopEvent = INVALID_HANDLE_VALUE;
};
