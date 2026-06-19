package fr.javatic.ezvizEnableRtsp.hikvisionSdk;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.jna.NativeLong;
import fr.javatic.ezvizEnableRtsp.hikvisionSdk.bindings.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

public class Hikvision implements AutoCloseable {
    private static final Logger LOGGER = LoggerFactory.getLogger(Hikvision.class.getSimpleName());
    private static final HCNetSDKNative hCNetSDK = HCNetSDKNative.INSTANCE;

    private final ObjectMapper objectMapper;
    private NativeLong currentUser = null;

    public Hikvision() {
        LOGGER.info("Initialize SDK");
        objectMapper = new ObjectMapper();

        hCNetSDK.NET_DVR_Init();
        hCNetSDK.NET_DVR_SetLogToFile(3, "/home/pi/ezviz_rtsp/sdklog", false);
        if (!hCNetSDK.NET_DVR_SetConnectTime(2500, 1)) {   // 1 conexión/login, timeout corto para reintentar rápido
            LOGGER.error("Call to NET_DVR_SetConnectTime failed");
        }
        hCNetSDK.NET_DVR_SetReconnect(10000, true);
    }

    public void login(String cameraHost, short cameraPort, String username, String password) {
        LOGGER.info("Perform Login");
        if (currentUser != null) {
            throw new IllegalStateException("User is already logged in");
        }

        var loginInfo = new NET_DVR_USER_LOGIN_INFO();
        var deviceInfo = new NET_DVR_DEVICEINFO_V40();

        copyStringTo(cameraHost, loginInfo.sDeviceAddress);
        copyStringTo(username, loginInfo.sUserName);
        copyStringTo(password, loginInfo.sPassword);
        loginInfo.wPort = cameraPort;
        loginInfo.byLoginMode = (byte) Integer.parseInt(System.getenv().getOrDefault("EZVIZ_LOGINMODE", "0"));
        loginInfo.byHttps = (byte) Integer.parseInt(System.getenv().getOrDefault("EZVIZ_HTTPS", "0"));
        LOGGER.info("Login mode byLoginMode={} byHttps={}", loginInfo.byLoginMode, loginInfo.byHttps);
        loginInfo.write();

        NativeLong userId = hCNetSDK.NET_DVR_Login_V40(loginInfo.getPointer(), deviceInfo.getPointer());
        int loginErr = hCNetSDK.NET_DVR_GetLastError();
        int uid32 = (int) userId.longValue();   // el LONG de Hikvision es de 32 bits
        LOGGER.info("Login_V40 userId={} (32b={}) lastErr={}", userId.longValue(), uid32, loginErr);
        if (uid32 == -1) {
            throw new HikvisionCallFailure("Login failed : " + loginErr);
        }

        this.currentUser = new NativeLong(uid32);   // normaliza a 32 bits
    }

    public NetDvrXmlConfigOutput setServiceSwitch(ServiceSwitchConfig config) {
        LOGGER.info("Set service switch " + config);
        String body;
        try {
            body = objectMapper.writeValueAsString(new ServiceSwitchCommand(config));
        } catch (JsonProcessingException e) {
            throw new RuntimeException(e);
        }

        var url = System.getenv().getOrDefault("EZVIZ_URL", "PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json");
        if (System.getenv("EZVIZ_BODY") != null) body = System.getenv("EZVIZ_BODY");  // "" => sin cuerpo (GET)
        LOGGER.info("Request URL: " + url);
        LOGGER.info("Request body: " + body);
        var urlSp = new StringPointer(url);

        var xmlConfigInput = new NET_DVR_XML_CONFIG_INPUT();
        xmlConfigInput.dwSize = xmlConfigInput.size();
        xmlConfigInput.lpRequestUrl = urlSp.pointer;
        xmlConfigInput.dwRequestUrlLen = urlSp.size;
        if (body == null || body.isEmpty()) {
            xmlConfigInput.lpInBuffer = null;
            xmlConfigInput.dwInBufferSize = 0;
        } else {
            var bodySp = new StringPointer(body);
            xmlConfigInput.lpInBuffer = bodySp.pointer;
            xmlConfigInput.dwInBufferSize = bodySp.size;
        }
        xmlConfigInput.dwRecvTimeOut = 5000;
        xmlConfigInput.write();

        var xmlConfigOutput = new NET_DVR_XML_CONFIG_OUTPUT();
        xmlConfigOutput.dwSize = xmlConfigOutput.size();
        xmlConfigOutput.lpOutBuffer = new BYTE_ARRAY(10485760).getPointer();
        xmlConfigOutput.dwOutBufferSize = 10485760;
        xmlConfigOutput.lpStatusBuffer = new BYTE_ARRAY(16384).getPointer();
        xmlConfigOutput.dwStatusSize = 16384;
        xmlConfigOutput.write();

        boolean ok = hCNetSDK.NET_DVR_STDXMLConfig(this.currentUser, xmlConfigInput, xmlConfigOutput);
        int lastErr = hCNetSDK.NET_DVR_GetLastError();
        xmlConfigOutput.read();
        String statusDump = "";
        try {
            if (xmlConfigOutput.lpStatusBuffer != null) {
                byte[] sb = xmlConfigOutput.lpStatusBuffer.getByteArray(0, Math.min(xmlConfigOutput.dwStatusSize, 4096));
                statusDump = new String(sb, java.nio.charset.StandardCharsets.UTF_8).trim();
            }
        } catch (Exception ignore) {}
        String outDump = "";
        try {
            int n = xmlConfigOutput.dwReturnedXMLSize;
            if (xmlConfigOutput.lpOutBuffer != null && n > 0) {
                outDump = new String(xmlConfigOutput.lpOutBuffer.getByteArray(0, Math.min(n, 4096)),
                        java.nio.charset.StandardCharsets.UTF_8).trim();
            }
        } catch (Exception ignore) {}
        LOGGER.info("STDXMLConfig ok={} lastErr={} returnedXMLSize={} status=[{}] out=[{}]",
                ok, lastErr, xmlConfigOutput.dwReturnedXMLSize, statusDump, outDump);
        if (!ok && !"true".equals(System.getenv("EZVIZ_PROBE"))) {
            throw new HikvisionCallFailure("Set Service Switch failed : " + lastErr + " status=[" + statusDump + "]");
        }

        NetDvrXmlConfigOutput output;
        try {
            output = objectMapper.readValue(
                    xmlConfigOutput.lpOutBuffer.getByteArray(0L, xmlConfigOutput.dwReturnedXMLSize),
                    NetDvrXmlConfigOutput.class
            );
        } catch (IOException e) {
            throw new RuntimeException(e);
        }

        if (output.statusCode() != 1) {
            throw new HikvisionCallFailure("Set Service Switch failed : " + output);
        }

        return output;
    }

    public String rawStdXml(String url, String body) {
        // Modo "body en URL" (como el tool original): URL\r\nbody\r\n todo en lpRequestUrl, sin lpInBuffer
        if ("1".equals(System.getenv("EZVIZ_BODY_IN_URL")) && body != null && !body.isEmpty()) {
            url = url + "\r\n" + body + "\r\n";
            body = "";
        }
        var urlSp = new StringPointer(url);
        var inBuf = new BYTE_ARRAY((body == null || body.isEmpty()) ? 1 : body.getBytes(java.nio.charset.StandardCharsets.UTF_8).length);
        int inLen = 0;
        if (body != null && !body.isEmpty()) {
            byte[] bb = body.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            System.arraycopy(bb, 0, inBuf.byValue, 0, bb.length);
            inBuf.write();
            inLen = bb.length;
        }
        var xmlIn = new NET_DVR_XML_CONFIG_INPUT();
        xmlIn.dwSize = xmlIn.size();
        xmlIn.lpRequestUrl = urlSp.pointer;
        xmlIn.dwRequestUrlLen = urlSp.size;
        xmlIn.lpInBuffer = inBuf.getPointer();   // siempre no-nulo (evita err 17)
        xmlIn.dwInBufferSize = inLen;
        xmlIn.dwRecvTimeOut = 5000;
        xmlIn.write();

        var outBuf = new BYTE_ARRAY(1048576);
        var statusBuf = new BYTE_ARRAY(16384);
        var xmlOut = new NET_DVR_XML_CONFIG_OUTPUT();
        xmlOut.dwSize = xmlOut.size();
        xmlOut.lpOutBuffer = outBuf.getPointer();
        xmlOut.dwOutBufferSize = 1048576;
        xmlOut.lpStatusBuffer = statusBuf.getPointer();
        xmlOut.dwStatusSize = 16384;
        xmlOut.write();

        boolean ok = hCNetSDK.NET_DVR_STDXMLConfig(this.currentUser, xmlIn, xmlOut);
        int err = hCNetSDK.NET_DVR_GetLastError();
        xmlOut.read();
        String out = "", status = "";
        try { if (xmlOut.dwReturnedXMLSize > 0) out = new String(outBuf.getPointer().getByteArray(0, Math.min(xmlOut.dwReturnedXMLSize, 2048)), java.nio.charset.StandardCharsets.UTF_8).trim(); } catch (Exception e){}
        try { status = new String(statusBuf.getPointer().getByteArray(0, 512), java.nio.charset.StandardCharsets.UTF_8).trim(); } catch (Exception e){}
        java.lang.ref.Reference.reachabilityFence(urlSp);
        java.lang.ref.Reference.reachabilityFence(inBuf);
        java.lang.ref.Reference.reachabilityFence(outBuf);
        java.lang.ref.Reference.reachabilityFence(statusBuf);
        java.lang.ref.Reference.reachabilityFence(xmlIn);
        return "ok=" + ok + " err=" + err + " outLen=" + xmlOut.dwReturnedXMLSize + " out=[" + out + "] status=[" + status + "]";
    }

    public void logout() {
        LOGGER.info("Logout");
        if (currentUser != null) {
            boolean lo = hCNetSDK.NET_DVR_Logout(this.currentUser);
            LOGGER.info("NET_DVR_Logout -> {}", lo);
        }
        currentUser = null;
    }

    @Override
    public void close() throws Exception {
        LOGGER.info("Closing SDK");
        if (currentUser != null) {
            logout();
        }
        hCNetSDK.NET_DVR_Cleanup();
    }

    private static void copyStringTo(String data, byte[] target) {
        var dataBytes = data.getBytes(StandardCharsets.UTF_8);
        System.arraycopy(dataBytes, 0, target, 0, dataBytes.length);
    }
}
