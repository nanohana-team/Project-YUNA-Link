// Project YUNA Link - input_server.cpp

#include "driver_main.h"
#include "input_server.h"
#include <cstring>
#include <cstdio>
#include <thread>
#include <chrono>

InputServer::InputServer(SharedState* state) : m_state(state)
{ m_stopEvent = CreateEventA(nullptr,TRUE,FALSE,nullptr); }

InputServer::~InputServer()
{
    Stop();
    if (m_stopEvent!=INVALID_HANDLE_VALUE)
    { CloseHandle(m_stopEvent); m_stopEvent=INVALID_HANDLE_VALUE; }
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
    if (m_stopEvent!=INVALID_HANDLE_VALUE) SetEvent(m_stopEvent);
    if (m_thread.joinable()) m_thread.join();
}

void InputServer::ServerThread()
{
    while (m_running)
    {
        HANDLE hPipe = CreateNamedPipeA(YUNA_INPUT_PIPE,
            PIPE_ACCESS_INBOUND|FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,
            1, 0, 4096, 0, nullptr);
        if (hPipe==INVALID_HANDLE_VALUE)
        { WaitForSingleObject(m_stopEvent,1000); continue; }

        OVERLAPPED ov{}; ov.hEvent=CreateEventA(nullptr,TRUE,FALSE,nullptr);
        BOOL connected=ConnectNamedPipe(hPipe,&ov);
        DWORD err=GetLastError();
        if (!connected)
        {
            if (err==ERROR_IO_PENDING)
            {
                HANDLE h[2]={ov.hEvent,m_stopEvent};
                if (WaitForMultipleObjects(2,h,FALSE,INFINITE)!=WAIT_OBJECT_0)
                { CancelIo(hPipe); CloseHandle(ov.hEvent); CloseHandle(hPipe); break; }
                DWORD d; connected=GetOverlappedResult(hPipe,&ov,&d,FALSE);
            }
            else if (err==ERROR_PIPE_CONNECTED) connected=TRUE;
        }
        CloseHandle(ov.hEvent);
        if (!connected||!m_running) { DisconnectNamedPipe(hPipe); CloseHandle(hPipe); continue; }

        DriverLog("[YUNA Input] Client connected\n");
        char lineBuf[256]; int lineLen=0;
        while (m_running)
        {
            OVERLAPPED rov{}; rov.hEvent=CreateEventA(nullptr,TRUE,FALSE,nullptr);
            char ch=0; DWORD br=0;
            BOOL ok=ReadFile(hPipe,&ch,1,&br,&rov);
            if (!ok && GetLastError()==ERROR_IO_PENDING)
            {
                HANDLE h[2]={rov.hEvent,m_stopEvent};
                if (WaitForMultipleObjects(2,h,FALSE,INFINITE)!=WAIT_OBJECT_0)
                { CancelIo(hPipe); CloseHandle(rov.hEvent); goto disc; }
                ok=GetOverlappedResult(hPipe,&rov,&br,FALSE);
            }
            CloseHandle(rov.hEvent);
            if (!ok||br==0) break;
            if (ch=='\n'||ch=='\r')
            { if(lineLen>0){ lineBuf[lineLen]='\0'; HandleLine(lineBuf); lineLen=0; } }
            else if (lineLen<(int)sizeof(lineBuf)-1) lineBuf[lineLen++]=ch;
        }
    disc:
        m_state->cmdReset();
        DriverLog("[YUNA Input] Client disconnected, input reset\n");
        DisconnectNamedPipe(hPipe); CloseHandle(hPipe);
    }
    DriverLog("[YUNA Input] Thread exiting\n");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Parse target string -> device index  (0=HEAD 1=L_CONTROLLER 2=R_CONTROLLER)
// Returns -1 on unknown target.
static int parseTarget(const char* s)
{
    if (strcmp(s,"HEAD"        )==0) return 0;
    if (strcmp(s,"L_CONTROLLER")==0) return 1;
    if (strcmp(s,"R_CONTROLLER")==0) return 2;
    return -1;
}

// Parse position axis string -> 0=x 1=y 2=z  or -1
static int parsePosAxis(const char* s)
{
    if (s[0]=='x'||s[0]=='X') return 0;
    if (s[0]=='y'||s[0]=='Y') return 1;
    if (s[0]=='z'||s[0]=='Z') return 2;
    return -1;
}

// Parse rotation axis string -> 0=rx(pitch) 1=ry(yaw) 2=rz(roll)  or -1
// Accepts: rX rY rZ  or  X Y Z  (ROTATE context)
static int parseRotAxis(const char* s)
{
    // "rX" / "rx" style
    if ((s[0]=='r'||s[0]=='R') && s[1]!='\0')
    {
        char c = s[1];
        if (c=='x'||c=='X') return 0;
        if (c=='y'||c=='Y') return 1;
        if (c=='z'||c=='Z') return 2;
    }
    // bare "X" / "Y" / "Z" style (ROTATE command)
    if (s[0]=='x'||s[0]=='X') return 0;
    if (s[0]=='y'||s[0]=='Y') return 1;
    if (s[0]=='z'||s[0]=='Z') return 2;
    return -1;
}

static const char* axisName(int a){ return a==0?"X":a==1?"Y":"Z"; }
static const char* rotAxisName(int a){ return a==0?"pitch(rX)":a==1?"yaw(rY)":"roll(rZ)"; }
static const char* targetName(int d){ return d==0?"HEAD":d==1?"L_CONTROLLER":"R_CONTROLLER"; }

// ---------------------------------------------------------------------------
// Command parser
//
// Pose commands:
//   MOVE   HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z  <delta>
//   ROTATE HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z|rX|rY|rZ  <delta_deg>
//   SET    HEAD|L_CONTROLLER|R_CONTROLLER  x|y|z   <value>     (absolute pos)
//   SET    HEAD|L_CONTROLLER|R_CONTROLLER  rX|rY|rZ <value_deg> (absolute rot)
//   RESET_POSE HEAD|L_CONTROLLER|R_CONTROLLER
//
// Input commands (unchanged):
//   SET START|MENU|A|B|X|Y 0|1
//   SET RTRIGGER|RGRIP|LTRIGGER|LGRIP <f>
//   SET L_STICK|R_STICK <x> <y>
//   TAP A|B|X|Y|START|MENU
//   RESET_INPUT
// ---------------------------------------------------------------------------
void InputServer::HandleLine(const char* line)
{
    while (*line==' ') ++line;
    if (!*line) return;

    // ----------------------------------------------------------------
    // RESET_INPUT
    // ----------------------------------------------------------------
    if (strcmp(line,"RESET_INPUT")==0)
    { m_state->cmdReset(); DriverLog("[INPUT] RESET_INPUT\n"); return; }

    // ----------------------------------------------------------------
    // RESET_POSE <target>
    // ----------------------------------------------------------------
    if (strncmp(line,"RESET_POSE ",11)==0)
    {
        int dev = parseTarget(line+11);
        if (dev<0){ DriverLog("[POSE] Unknown target: %s\n",line+11); return; }
        m_state->cmdResetPose(dev);
        DriverLog("[POSE] RESET_POSE %s\n", targetName(dev));
        return;
    }

    // ----------------------------------------------------------------
    // MOVE <target> <x|y|z> <delta>
    //   e.g. MOVE HEAD y 1
    //        MOVE L_CONTROLLER x -0.05
    // ----------------------------------------------------------------
    if (strncmp(line,"MOVE ",5)==0)
    {
        char tgt[32], axs[8]; double delta=0.;
        if (sscanf_s(line+5, "%31s %7s %lf", tgt,(unsigned)sizeof(tgt),
                                              axs,(unsigned)sizeof(axs), &delta)!=3)
        { DriverLog("[POSE] MOVE parse error: %s\n",line); return; }

        int dev  = parseTarget(tgt);
        int axis = parsePosAxis(axs);
        if (dev<0)  { DriverLog("[POSE] Unknown target: %s\n", tgt); return; }
        if (axis<0) { DriverLog("[POSE] Unknown axis: %s\n",   axs); return; }

        m_state->cmdMove(dev, axis, delta);
        DriverLog("[POSE] MOVE %s %s %+.4f\n", targetName(dev), axisName(axis), delta);
        return;
    }

    // ----------------------------------------------------------------
    // ROTATE <target> <x|y|z|rX|rY|rZ> <delta_deg>
    //   e.g. ROTATE HEAD Z 90
    //        ROTATE R_CONTROLLER rY -45
    // ----------------------------------------------------------------
    if (strncmp(line,"ROTATE ",7)==0)
    {
        char tgt[32], axs[8]; double delta=0.;
        if (sscanf_s(line+7, "%31s %7s %lf", tgt,(unsigned)sizeof(tgt),
                                              axs,(unsigned)sizeof(axs), &delta)!=3)
        { DriverLog("[POSE] ROTATE parse error: %s\n",line); return; }

        int dev  = parseTarget(tgt);
        int axis = parseRotAxis(axs);
        if (dev<0)  { DriverLog("[POSE] Unknown target: %s\n", tgt); return; }
        if (axis<0) { DriverLog("[POSE] Unknown axis: %s\n",   axs); return; }

        m_state->cmdRotate(dev, axis, delta);
        DriverLog("[POSE] ROTATE %s %s %+.2f deg\n", targetName(dev), rotAxisName(axis), delta);
        return;
    }

    // ----------------------------------------------------------------
    // SET <key> ...
    // ----------------------------------------------------------------
    if (strncmp(line,"SET ",4)==0)
    {
        const char* r = line+4;

        // SET <target> <axis> <value>  -- pose absolute set
        // Try to match  HEAD|L_CONTROLLER|R_CONTROLLER first token
        char tgt[32], axs[8]; double val=0.;
        if (sscanf_s(r, "%31s %7s %lf", tgt,(unsigned)sizeof(tgt),
                                        axs,(unsigned)sizeof(axs), &val)==3)
        {
            int dev = parseTarget(tgt);
            if (dev>=0)
            {
                // Position axis?
                int pax = parsePosAxis(axs);
                if (pax>=0)
                {
                    m_state->cmdSetPose(dev, pax, val);
                    DriverLog("[POSE] SET %s %s %.4f\n", targetName(dev), axisName(pax), val);
                    return;
                }
                // Rotation axis? (rX rY rZ style)
                int rax = parseRotAxis(axs);
                if (rax>=0 && (axs[0]=='r'||axs[0]=='R'))
                {
                    m_state->cmdSetPose(dev, rax+3, val);
                    DriverLog("[POSE] SET %s %s %.2f deg\n", targetName(dev), rotAxisName(rax), val);
                    return;
                }
            }
        }

        // Fallthrough to input commands (single-token key)
        auto boolVal = [](const char* s){ return *s!='0'; };

        if (strncmp(r,"START ",6)==0)
        { bool v=boolVal(r+6); m_state->cmdSetStart(v); DriverLog("[INPUT] START=%d\n",(int)v); return; }
        if (strncmp(r,"MENU ",5)==0)
        { bool v=boolVal(r+5); m_state->cmdSetMenu(v);  DriverLog("[INPUT] MENU=%d\n",(int)v);  return; }
        if (strncmp(r,"A ",2)==0)
        { bool v=boolVal(r+2); m_state->cmdSetA(v); DriverLog("[INPUT] A=%d\n",(int)v); return; }
        if (strncmp(r,"B ",2)==0)
        { bool v=boolVal(r+2); m_state->cmdSetB(v); DriverLog("[INPUT] B=%d\n",(int)v); return; }
        if (strncmp(r,"X ",2)==0)
        { bool v=boolVal(r+2); m_state->cmdSetX(v); DriverLog("[INPUT] X=%d\n",(int)v); return; }
        if (strncmp(r,"Y ",2)==0)
        { bool v=boolVal(r+2); m_state->cmdSetY(v); DriverLog("[INPUT] Y=%d\n",(int)v); return; }

        if (strncmp(r,"RTRIGGER ",9)==0)
        { float v=0; sscanf_s(r+9,"%f",&v); m_state->cmdSetRTrigger(v); DriverLog("[INPUT] RTRIGGER=%.2f\n",v); return; }
        if (strncmp(r,"RGRIP ",6)==0)
        { float v=0; sscanf_s(r+6,"%f",&v); m_state->cmdSetRGrip(v);    DriverLog("[INPUT] RGRIP=%.2f\n",v);    return; }
        if (strncmp(r,"LTRIGGER ",9)==0)
        { float v=0; sscanf_s(r+9,"%f",&v); m_state->cmdSetLTrigger(v); DriverLog("[INPUT] LTRIGGER=%.2f\n",v); return; }
        if (strncmp(r,"LGRIP ",6)==0)
        { float v=0; sscanf_s(r+6,"%f",&v); m_state->cmdSetLGrip(v);    DriverLog("[INPUT] LGRIP=%.2f\n",v);    return; }

        if (strncmp(r,"L_STICK ",8)==0)
        { float x=0,y=0; if(sscanf_s(r+8,"%f %f",&x,&y)==2){ m_state->cmdSetLeftStick(x,y); DriverLog("[INPUT] L_STICK=(%.2f,%.2f)\n",x,y); } return; }
        if (strncmp(r,"R_STICK ",8)==0)
        { float x=0,y=0; if(sscanf_s(r+8,"%f %f",&x,&y)==2){ m_state->cmdSetRightStick(x,y); DriverLog("[INPUT] R_STICK=(%.2f,%.2f)\n",x,y); } return; }
    }

    // ----------------------------------------------------------------
    // TAP <button>
    // ----------------------------------------------------------------
    if (strncmp(line,"TAP ",4)==0)
    {
        const char* btn = line+4;
        auto pulse = [&](auto setFn){
            setFn(true);
            std::thread([this,setFn](){
                std::this_thread::sleep_for(std::chrono::milliseconds(120));
                setFn(false);
            }).detach();
        };
        if      (strcmp(btn,"A"    )==0){ DriverLog("[INPUT] TAP A\n");     pulse([this](bool v){ m_state->cmdSetA(v);     }); }
        else if (strcmp(btn,"B"    )==0){ DriverLog("[INPUT] TAP B\n");     pulse([this](bool v){ m_state->cmdSetB(v);     }); }
        else if (strcmp(btn,"X"    )==0){ DriverLog("[INPUT] TAP X\n");     pulse([this](bool v){ m_state->cmdSetX(v);     }); }
        else if (strcmp(btn,"Y"    )==0){ DriverLog("[INPUT] TAP Y\n");     pulse([this](bool v){ m_state->cmdSetY(v);     }); }
        else if (strcmp(btn,"START")==0){ DriverLog("[INPUT] TAP START\n"); pulse([this](bool v){ m_state->cmdSetStart(v); }); }
        else if (strcmp(btn,"MENU" )==0){ DriverLog("[INPUT] TAP MENU\n");  pulse([this](bool v){ m_state->cmdSetMenu(v);  }); }
        else DriverLog("[INPUT] Unknown TAP: %s\n", btn);
        return;
    }

    DriverLog("[INPUT] Unknown command: %s\n", line);
}
