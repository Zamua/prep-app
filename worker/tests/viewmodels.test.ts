import { describe, expect, it } from "vitest";
import { DERIVED_TEMPLATES, derive } from "../app/viewmodels/derive";
import { deriveSettingsAgent } from "../app/viewmodels/settingsAgent";
import { deriveTransformProgress } from "../app/viewmodels/transformProgress";
import { deriveWorkflowBadge } from "../app/viewmodels/workflowBadge";

describe("transform progress groupings", () => {
  const context = {
    progress: {
      plan: {
        additions: [{ dest_deck: "distsys" }, { dest_deck: "capitals" }, { dest_deck: "distsys" }, { dest_deck: null }],
        deletions: [101, 102, 103, 999],
        card_moves: [
          { question_id: 201, dest_deck: "consensus" },
          { question_id: 202, dest_deck: "consensus" },
          { question_id: 44, dest_deck: "capitals" },
          { question_id: 777, dest_deck: "capitals" },
        ],
      },
    },
    modification_diffs: [{ deck_name: "capitals" }, { deck_name: "distsys" }, { deck_name: "" }, { deck_name: "capitals" }],
    deletion_decks: { "101": "capitals", "102": "distsys", "103": "capitals" },
    move_source_decks: { "201": "distsys", "202": "distsys", "44": "distsys" },
  };
  const fields = deriveTransformProgress(context);

  it("groups modifications by deck in first-seen order, blanks as (unknown)", () => {
    expect(fields.mods_by_deck.map(([label, diffs]) => [label, diffs.length])).toEqual([
      ["capitals", 2],
      ["distsys", 1],
      ["(unknown)", 1],
    ]);
  });
  it("groups additions by destination", () => {
    expect(fields.adds_by_deck.map(([label, adds]) => [label, adds.length])).toEqual([
      ["distsys", 2],
      ["capitals", 1],
      ["(unknown)", 1],
    ]);
  });
  it("groups deletions through the id-to-deck table, keys as JSON left them", () => {
    expect(fields.dels_by_deck).toEqual([
      ["capitals", [101, 103]],
      ["distsys", [102]],
      ["(unknown)", [999]],
    ]);
  });
  it("groups moves by source and destination pair, ids only", () => {
    expect(fields.move_groups).toEqual([
      ["distsys → consensus", [201, 202]],
      ["distsys → capitals", [44]],
      ["(unknown) → capitals", [777]],
    ]);
  });
  it("is empty without a plan", () => {
    expect(deriveTransformProgress({ progress: { plan: null }, modification_diffs: [] })).toEqual({
      mods_by_deck: [],
      adds_by_deck: [],
      dels_by_deck: [],
      move_groups: [],
    });
    expect(deriveTransformProgress({}).move_groups).toEqual([]);
  });
});

describe("settings agent", () => {
  it("counts the sections holding a key", () => {
    expect(deriveSettingsAgent({ byok_sections: [{ metadata: null }, { metadata: { key_prefix: "sk" } }, {}] })).toEqual({ byok_connected_count: 1 });
    expect(deriveSettingsAgent({})).toEqual({ byok_connected_count: 0 });
  });
});

describe("workflow badge", () => {
  it("counts the non-terminal workflows", () => {
    expect(deriveWorkflowBadge({ workflows: [{ is_terminal: false }, { is_terminal: true }, {}] })).toEqual({ active_workflow_count: 2 });
    expect(deriveWorkflowBadge({ workflows: [] })).toEqual({ active_workflow_count: 0 });
  });
});

describe("derive", () => {
  it("adds the fields for the templates that moved logic out, copies the rest", () => {
    expect(DERIVED_TEMPLATES).toEqual([
      "transform.html",
      "partials/transform_progress.html",
      "settings_agent.html",
      "partials/workflow_badge.html",
    ]);
    const ctx = { workflows: [{ is_terminal: false }] };
    expect(derive("partials/workflow_badge.html", ctx)).toEqual({ ...ctx, active_workflow_count: 1 });
    expect(derive("partials/workflow_badge.html", ctx)).not.toBe(ctx);
    expect(derive("error.html", { status_code: 404 })).toEqual({ status_code: 404 });
  });
  it("does not mutate its input", () => {
    const ctx = { byok_sections: [{ metadata: 1 }] };
    derive("settings_agent.html", ctx);
    expect(ctx).toEqual({ byok_sections: [{ metadata: 1 }] });
  });
});
