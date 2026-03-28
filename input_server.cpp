// Project YUNA Link - input_server.cpp

#include "driver_main.h"
#include "input_server.h"
#include <cstring>
#include <cstdio>
#include <thread>
#include <chrono>

InputServer::InputServer(SharedState* state)
    : m_state(state)
{
    m_stopEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
}

InputServer::~InputServer()
{
    Stop();
    if (m_stopEvent != INVALID_HANDLE_VALUE)
    { CloseHandle(m_stopEvent); m_stopEvent = INVALID_HANDLE_VALUE; }
}

void InputServer::Start()
{
    if (m_running.exchange(true)) return;
    ResetEvent(m_stopEvent);
    m_thread = std::thread(&InputServer::ServerThread, this);
    DriverLog("[YUNA Input] Listening on %s\n", YUNA_INPUT_PIPE);
}

void InputServer::Stop()
{
    if (!m_running.exchange(false)) return;
    if (m_stopEvent != INVALID_HANDLE_VALUE) SetEvent(m_stopEvent);
    if (m_thread.joinable()) m_thread.join();
}

void InputServer::ServerThread()
{
    while (m_running)
    {
        HANDLE hPipe = CreateNamedPipeA(
            YUNA_INPUT_PIPE,
            PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, 0, 4096, 0, nullptr);

        if (hPipe == INVALID_HANDLE_VALUE)
        {
            WaitForSingleObject(m_stopEvent, 1000);
            continue;
        }

        OVERLAPPED ov{};
        ov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
        BOOL connected = ConnectNamedPipe(hPipe, &ov);
        DWORD err = GetLastError();

        if (!connected)
        {
            if (err == ERROR_IO_PENDING)
            {
                HANDLE h[2] = { ov.hEvent, m_stopEvent };
                if (WaitForMultipleObjects(2, h, FALSE, INFINITE) != WAIT_OBJECT_0)
                {
                    CancelIo(hPipe);
                    CloseHandle(ov.hEvent); CloseHandle(hPipe);
                    break;
                }
                DWORD d;
                connected = GetOverlappedResult(hPipe, &ov, &d, FALSE);
            }
            else if (err == ERROR_PIPE_CONNECTED) connected = TRUE;
        }
        CloseHandle(ov.hEvent);

        if (!connected || !m_running)
        { DisconnectNamedPipe(hPipe); CloseHandle(hPipe); continue; }

        DriverLog("[YUNA Input] Client connected\n");

        char lineBuf[256]; int lineLen = 0;
        while (m_running)
        {
            OVERLAPPED rov{};
            rov.hEvent = CreateEventA(nullptr, TRUE, FALSE, nullptr);
            char ch = 0; DWORD br = 0;
            BOOL ok = ReadFile(hPipe, &ch, 1, &br, &rov);
            if (!ok && GetLastError() == ERROR_IO_PENDING)
            {
                HANDLE h[2] = { rov.hEvent, m_stopEvent };
                if (WaitForMultipleObjects(2, h, FALSE, INFINITE) != WAIT_OBJECT_0)
                { CancelIo(hPipe); CloseHandle(rov.hEvent); goto disc; }
                ok = GetOverlappedResult(hPipe, &rov, &br, FALSE);
            }
            CloseHandle(rov.hEvent);
            if (!ok || br == 0) break;

            if (ch == '\n' || ch == '\r')
            {
                if (lineLen > 0)
                { lineBuf[lineLen] = '\0'; HandleLine(lineBuf); lineLen = 0; }
            }
            else if (lineLen < (int)sizeof(lineBuf)-1)
                lineBuf[lineLen++] = ch;
        }

    disc:
        m_state->cmdReset();
        DriverLog("[YUNA Input] Client disconnected, input reset\n");
        DisconnectNamedPipe(hPipe); CloseHandle(hPipe);
    }
    DriverLog("[YUNA Input] Thread exiting\n");
}

void InputServer::HandleLine(const char* line)
{
    while (*line == ' ') ++line;
    if (*line == '\0') return;

    if (strcmp(line, "RESET_INPUT") == 0)
    { m_state->cmdReset(); DriverLog("[INPUT] RESET_INPUT\n"); return; }

    if (strcmp(line, "TAP A") == 0)
    {
        m_state->cmdSetA(true);
        DriverLog("[INPUT] TAP A\n");
        std::thread([this](){
            std::this_thread::sleep_for(std::chrono::milliseconds(66));
            m_state->cmdSetA(false);
        }).detach();
        return;
    }

    if (strcmp(line, "TAP START") == 0)
    {
        m_state->cmdSetStart(true);
        DriverLog("[INPUT] TAP START\n");
        std::thread([this](){
            std::this_thread::sleep_for(std::chrono::milliseconds(66));
            m_state->cmdSetStart(false);
        }).detach();
        return;
    }

    if (strncmp(line, "SET ", 4) == 0)
    {
        const char* r = line + 4;
        if (strncmp(r, "START ", 6) == 0)
        { bool v = r[6]!='0'; m_state->cmdSetStart(v); DriverLog("[INPUT] START=%d\n",(int)v); return; }
        if (strncmp(r, "A ", 2) == 0)
        { bool v = r[2]!='0'; m_state->cmdSetA(v); DriverLog("[INPUT] A=%d\n",(int)v); return; }
        if (strncmp(r, "L_STICK ", 8) == 0)
        { float x=0,y=0; if(sscanf_s(r+8,"%f %f",&x,&y)==2){ m_state->cmdSetLeftStick(x,y); DriverLog("[INPUT] L_STICK=(%.2f,%.2f)\n",x,y); } return; }
        if (strncmp(r, "R_STICK ", 8) == 0)
        { float x=0,y=0; if(sscanf_s(r+8,"%f %f",&x,&y)==2){ m_state->cmdSetRightStick(x,y); DriverLog("[INPUT] R_STICK=(%.2f,%.2f)\n",x,y); } return; }
    }

    DriverLog("[INPUT] Unknown: %s\n", line);
}
