package fr.javatic.ezvizEnableRtsp.hikvisionSdk;

import com.sun.jna.Pointer;
import fr.javatic.ezvizEnableRtsp.hikvisionSdk.bindings.BYTE_ARRAY;

import java.nio.charset.StandardCharsets;

class StringPointer {
    public final Pointer pointer;
    public final int size;
    // Mantener referencia viva: si el GC libera el BYTE_ARRAY, su memoria nativa
    // se libera y el puntero pasado al SDK queda inválido (err 17/11 aleatorios).
    public final BYTE_ARRAY backing;

    public StringPointer(String data) {
        var dataBytes = data.getBytes(StandardCharsets.UTF_8);

        this.backing = new BYTE_ARRAY(dataBytes.length);
        System.arraycopy(dataBytes, 0, backing.byValue, 0, dataBytes.length);
        backing.write();
        this.pointer = backing.getPointer();
        this.size = dataBytes.length;
    }
}
