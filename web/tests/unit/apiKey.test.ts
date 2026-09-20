import { afterEach, describe, expect, it } from "vitest";
import { authHeaders, KEY_HEADER, KEY_STORAGE, readKey, subscribeKey, withKey, writeKey } from "@/api/client";

/**
 * The API key lives in this browser's localStorage and rides on every request as X-Api-Key. With no key the
 * app behaves exactly as before: no header, no error. Storage that is missing or refuses reads as "no key".
 */

afterEach(() => {
  writeKey(null);
});

describe("the API key", () => {
  it("is absent by default, so a request carries no header and the app works as it did", () => {
    expect(readKey()).toBeNull();
    expect(authHeaders()).toEqual({});
    const req = withKey(new Request("http://127.0.0.1:8787/api/whoami"));
    expect(req.headers.has(KEY_HEADER)).toBe(false);
  });

  it("is kept in localStorage, trimmed, and sent as X-Api-Key once set", () => {
    let notified = 0;
    const off = subscribeKey(() => {
      notified += 1;
    });
    writeKey("  geo-key  ");
    expect(readKey()).toBe("geo-key");
    expect(window.localStorage.getItem(KEY_STORAGE)).toBe("geo-key");
    expect(authHeaders()).toEqual({ [KEY_HEADER]: "geo-key" });
    const req = withKey(new Request("http://127.0.0.1:8787/api/jobs", { method: "POST" }));
    expect(req.headers.get(KEY_HEADER)).toBe("geo-key");
    expect(notified).toBe(1);
    off();
  });

  it("does not overwrite a header a caller set on purpose", () => {
    writeKey("stored-key");
    const req = withKey(new Request("http://x/", { headers: { [KEY_HEADER]: "explicit" } }));
    expect(req.headers.get(KEY_HEADER)).toBe("explicit");
  });

  it("clears on an empty write", () => {
    writeKey("k");
    writeKey("   ");
    expect(readKey()).toBeNull();
    expect(window.localStorage.getItem(KEY_STORAGE)).toBeNull();
  });

  it("reads as no key when storage refuses, rather than breaking the page", () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    try {
      expect(readKey()).toBeNull();
      expect(() => writeKey("k")).not.toThrow();
      expect(authHeaders()).toEqual({});
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original);
    }
  });
});
