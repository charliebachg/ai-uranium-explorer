import { describe, expect, it } from "vitest";
import { ReviewItem, ReviewQueuePage } from "@/data/contract";

/** The review queue contract: an item carries both readings (either may be null), boxes are page-normalised, and
 * a resolution never replaces the readings it decided between. */

const readingA = {
  field: "to_depth",
  scope: "cell",
  as_printed: "42.0",
  bbox: [0.1, 0.1, 0.2, 0.12],
  row_bbox: [0.05, 0.1, 0.9, 0.12],
  row_index: 1,
  value_id: "x:74H09-0039:9f2c1a7b",
  model: "claude-opus-5",
};
const item = {
  queue_id: "abc123",
  file_num: "74H09-0039",
  page: 4,
  field: "to_depth",
  field_type: "depth",
  value_id: "x:74H09-0039:9f2c1a7b",
  reading_a: readingA,
  reading_b: { ...readingA, as_printed: "42.5", bbox: null, value_id: null, model: "z-ai/glm-5.3-flash" },
  reason: "disagreed",
  status: "open",
  run_id: "r1",
  created_at: "2026-09-21T00:00:00Z",
};

describe("review queue contract", () => {
  it("parses an open item with a reading on each side", () => {
    const r = ReviewItem.safeParse(item);
    expect(r.success).toBe(true);
    expect(r.data?.reading_b?.bbox).toBeNull();
    expect(r.data?.reading_b?.row_bbox).toEqual([0.05, 0.1, 0.9, 0.12]);
  });

  it("allows one reading to be missing, and keeps both after a resolution", () => {
    const only = {
      ...item,
      reading_a: null,
      reason: "only_b",
      status: "accepted_b",
      resolved_by: "key:1a2b3c4d",
      resolved_at: "2026-09-21T00:01:00Z",
      resolution: { decision: "accepted_b", reading: item.reading_b },
    };
    const r = ReviewItem.safeParse(only);
    expect(r.success).toBe(true);
    expect(r.data?.reading_a).toBeNull();
    expect(r.data?.reading_b?.as_printed).toBe("42.5");
  });

  it("rejects a box outside the page and an unknown status", () => {
    expect(ReviewItem.safeParse({ ...item, reading_a: { ...readingA, bbox: [0, 0, 1.2, 1] } }).success).toBe(
      false,
    );
    expect(ReviewItem.safeParse({ ...item, status: "maybe" }).success).toBe(false);
  });

  it("parses a page of items with its counts", () => {
    const page = {
      items: [item],
      total: 1,
      limit: 25,
      offset: 0,
      status: "open",
      file_num: null,
      counts: { open: 1 },
    };
    expect(ReviewQueuePage.safeParse(page).success).toBe(true);
  });
});
