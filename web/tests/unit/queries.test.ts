import { describe, expect, it } from "vitest";
import { keys, serviceState } from "@/features/prospect/queries";

describe("service state", () => {
  it("is unknown while the first probe is in flight, then up or down", () => {
    expect(serviceState({ isPending: true })).toBe("unknown");
    expect(serviceState({ isPending: false, data: true })).toBe("up");
    expect(serviceState({ isPending: false, data: false })).toBe("down");
    expect(serviceState({ isPending: false })).toBe("down");
  });

  it("names a conversation list by its cell so a new turn can invalidate exactly that list", () => {
    expect(keys.conversations("0201_0072")).toEqual(["service", "conversations", "0201_0072"]);
    expect(keys.evidence("0201_0072")).not.toEqual(keys.conversations("0201_0072"));
  });
});
