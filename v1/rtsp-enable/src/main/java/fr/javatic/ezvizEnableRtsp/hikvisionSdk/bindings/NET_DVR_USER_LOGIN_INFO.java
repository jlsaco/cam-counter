package fr.javatic.ezvizEnableRtsp.hikvisionSdk.bindings;

import com.sun.jna.Pointer;
import com.sun.jna.Structure;

import java.util.List;

public class NET_DVR_USER_LOGIN_INFO extends Structure {
    public byte[] sDeviceAddress = new byte[129];
    public byte byUseTransport;
    public short wPort;
    public byte[] sUserName = new byte[64];
    public byte[] sPassword = new byte[64];
    public CallbackLoginResult cbLoginResult;
    Pointer pUser;
    public int bUseAsynLogin;
    public byte byProxyType;
    public byte byUseUTCTime;
    public byte byLoginMode;   // 0=Privado, 1=ISAPI, 2=adaptativo
    public byte byHttps;       // 0=sin TLS, 1=TLS, 2=adaptativo
    public int iProxyID;
    public byte byVerifyMode;  // 0=no verificar
    public byte[] byRes2 = new byte[119];

    @Override
    public List<String> getFieldOrder() {
        return List.of(
                "sDeviceAddress",
                "byUseTransport",
                "wPort",
                "sUserName",
                "sPassword",
                "cbLoginResult",
                "bUseAsynLogin",
                "byProxyType",
                "byUseUTCTime",
                "byLoginMode",
                "byHttps",
                "iProxyID",
                "byVerifyMode",
                "byRes2"
        );
    }
}
