#pragma once
// Project YUNA Link - input_server.h
// Text command receiver on \\.\pipe\YunaLinkInput
//
// Commands:
//   SET START 0|1
//   SET A 0|1
//   SET L_STICK <x> <y>
//   SET R_STICK <x> <y>
//   RESET_INPUT
//   TAP START
//   TAP A

#include "shared_state.h"
#include <windows.h>
#include <thread>
#include <atomic>

#define YUNA_INPUT_PIPE R"(\\.\pipe\YunaLinkInput)"

class InputServer
{
public:
    explicit InputServer(SharedState* state);
    ~InputServer();

    void Start();
    void Stop();

private:
    void ServerThread();
    void HandleLine(const char* line);

    SharedState*       m_state;
    std::thread        m_thread;
    std::atomic<bool>  m_running{ false };
    HANDLE             m_stopEvent = INVALID_HANDLE_VALUE;
};
