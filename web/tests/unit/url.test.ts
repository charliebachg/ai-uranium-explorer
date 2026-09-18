import { describe, expect, it } from "vitest";
import { type LayerGroupId, layerGroups } from "@/config/layers";
import { decodeUrl, encodeUrl } from "@/state/url";

describe("url state", () => {
  it("round-trips camera, basemap, layers, filter and selection", () => {
    const visible = Object.fromEntries(layerGroups().map((g) => [g.id, g.id !== "nts"])) as Record<
      LayerGroupId,
      boolean
    >;
    const state = {
      camera: { center: [-104.51234, 58.12345] as [number, number], zoom: 11.234, bearing: 12.34, pitch: 35 },
      basemap: "ofm-dark" as const,
      theme: "dark" as const,
      visible,
      uraniumOnly: true,
      selected: { dataset: "compilation" as const, id: 89928, props: {}, lngLat: [0, 0] as [number, number] },
    };
    const back = decodeUrl(encodeUrl(state));
    expect(back.camera?.center[0]).toBeCloseTo(-104.5123, 4);
    expect(back.camera?.zoom).toBeCloseTo(11.23, 2);
    expect(back.basemap).toBe("ofm-dark");
    expect(back.visible?.nts).toBe(false);
    expect(back.visible?.compilation).toBe(true);
    expect(back.uraniumOnly).toBe(true);
    // the dark theme is the default, so it stays out of the URL; light is carried so a link keeps it
    expect(back.theme).toBeUndefined();
    expect(decodeUrl(encodeUrl({ ...state, theme: "light" })).theme).toBe("light");
    expect(back.selected).toEqual({ dataset: "compilation", id: 89928 });
  });

  it("ignores junk", () => {
    expect(decodeUrl("?c=a,b,c&b=nope&s=xyz:1")).toEqual({});
  });
});
