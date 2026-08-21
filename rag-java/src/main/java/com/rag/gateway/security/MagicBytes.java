package com.rag.gateway.security;

import java.nio.charset.StandardCharsets;

/**
 * 文件魔数校验：验证文件头与声明的扩展名一致（防伪装类型上传）。
 *
 * 原则：有魔数的类型必须匹配；txt/md 等无魔数类型放行（无法校验）。
 * PDF 魔数允许出现在文件头 1024 字节内（规范允许前导字节）。
 */
public final class MagicBytes {

    private static final byte[] PDF = "%PDF-".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] PNG = {(byte) 0x89, 0x50, 0x4E, 0x47};
    private static final byte[] JPEG = {(byte) 0xFF, (byte) 0xD8, (byte) 0xFF};
    /** docx/xlsx/pptx 均为 zip 容器，PK\x03\x04 是 zip 的标准文件头。 */
    private static final byte[] ZIP = {0x50, 0x4B, 0x03, 0x04};

    private MagicBytes() {
    }

    /**
     * @param head 文件头字节（最多 1024 字节）
     * @param len  head 有效长度
     * @param ext  小写扩展名（带点，如 .pdf，即 extractExt 的输出）
     */
    public static boolean matches(byte[] head, int len, String ext) {
        switch (ext) {
            case ".pdf":
                return containsAtStart(head, len, PDF);
            case ".png":
                return startsWith(head, len, PNG);
            case ".jpg":
            case ".jpeg":
                return startsWith(head, len, JPEG);
            case ".docx":
            case ".xlsx":
            case ".pptx":
                return startsWith(head, len, ZIP);
            default:
                return true; // txt/md 等无魔数，放行
        }
    }

    /** 在头部范围内查找魔数（PDF 允许前导字节，从头开始扫描）。 */
    private static boolean containsAtStart(byte[] head, int len, byte[] magic) {
        for (int i = 0; i + magic.length <= len; i++) {
            if (startsWith(head, i, len, magic)) {
                return true;
            }
        }
        return false;
    }

    private static boolean startsWith(byte[] head, int len, byte[] magic) {
        return startsWith(head, 0, len, magic);
    }

    private static boolean startsWith(byte[] head, int offset, int len, byte[] magic) {
        if (len - offset < magic.length) {
            return false;
        }
        for (int i = 0; i < magic.length; i++) {
            if (head[offset + i] != magic[i]) {
                return false;
            }
        }
        return true;
    }
}
