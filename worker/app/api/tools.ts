// The MCP tool catalog. An external client negotiates against these
// objects, so their text is part of the contract: reword a description and
// a model that was calling the tool correctly may stop.

export interface McpTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export const TOOLS: readonly McpTool[] = [
  {
    "description": "List every deck owned by the authenticated user. Returns each deck's name, type (srs|trivia), card count, due count, and pinned flag. Use prep_get_deck for richer per-deck detail.",
    "inputSchema": {
      "properties": {},
      "required": [],
      "type": "object"
    },
    "name": "prep_list_decks"
  },
  {
    "description": "Metadata for a single deck by name. Returns 404 if the user doesn't own a deck by that name.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_get_deck"
  },
  {
    "description": "Every card in a deck, with all fields (type, prompt, answer, choices, rubric, skeleton, language, regex, explanation). Same fields the CSV exporter emits.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_list_cards"
  },
  {
    "description": "Render an entire deck as a CSV text body — the same format prep's /deck/<name>/export.csv route produces. Anki-friendly.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_export_deck_csv"
  },
  {
    "description": "Create an empty SRS deck with the given name and optional context_prompt. Errors with 409 if a deck of that name already exists.",
    "inputSchema": {
      "properties": {
        "context_prompt": {
          "description": "Free-form description used as AI context.",
          "type": "string"
        },
        "name": {
          "description": "2-30 lowercase chars / digits / hyphens.",
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_create_deck"
  },
  {
    "description": "Append CSV rows to a deck (creates the deck if it doesn't exist). Expects the same column shape prep_export_deck_csv emits. Returns inserted / skipped_duplicates / errors.",
    "inputSchema": {
      "properties": {
        "csv": {
          "description": "Full CSV body with header row.",
          "type": "string"
        },
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name",
        "csv"
      ],
      "type": "object"
    },
    "name": "prep_import_csv"
  },
  {
    "description": "Rename a deck. The new name must be unused (otherwise 409). All cards / reviews / sessions follow the rename via the FK chain — no data migration.",
    "inputSchema": {
      "properties": {
        "name": {
          "description": "Current deck name.",
          "type": "string"
        },
        "new_name": {
          "description": "Target name. 2-30 lowercase chars / digits / hyphens.",
          "type": "string"
        }
      },
      "required": [
        "name",
        "new_name"
      ],
      "type": "object"
    },
    "name": "prep_rename_deck"
  },
  {
    "description": "Delete a deck and (via FK CASCADE) all its questions, cards, reviews, and study sessions. Irreversible — confirm before calling.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_delete_deck"
  },
  {
    "description": "Pin or unpin a deck on the user's index. Pinned decks float to the top of the library list.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        },
        "pinned": {
          "type": "boolean"
        }
      },
      "required": [
        "name",
        "pinned"
      ],
      "type": "object"
    },
    "name": "prep_set_deck_pinned"
  },
  {
    "description": "Set the AI-context prompt for a deck — used when prep generates new cards for this deck. Pass an empty string to clear.",
    "inputSchema": {
      "properties": {
        "context_prompt": {
          "type": "string"
        },
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name",
        "context_prompt"
      ],
      "type": "object"
    },
    "name": "prep_set_topic_prompt"
  },
  {
    "description": "Fetch a single card by its numeric id.",
    "inputSchema": {
      "properties": {
        "card_id": {
          "type": "integer"
        }
      },
      "required": [
        "card_id"
      ],
      "type": "object"
    },
    "name": "prep_get_card"
  },
  {
    "description": "Add a single card to a deck. Type must be one of short | mcq | multi | code. Required fields by type: short + code need prompt + answer; mcq + multi additionally need choices (array of strings). code optionally takes language + skeleton.",
    "inputSchema": {
      "properties": {
        "answer": {
          "description": "For mcq: the correct value. For multi: JSON-encoded array of correct values. For short / code: the canonical answer.",
          "type": "string"
        },
        "answer_regex": {
          "type": "string"
        },
        "choices": {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        "deck": {
          "description": "Deck name.",
          "type": "string"
        },
        "explanation": {
          "type": "string"
        },
        "language": {
          "type": "string"
        },
        "prompt": {
          "type": "string"
        },
        "rubric": {
          "type": "string"
        },
        "skeleton": {
          "type": "string"
        },
        "topic": {
          "type": "string"
        },
        "type": {
          "enum": [
            "short",
            "mcq",
            "multi",
            "code"
          ],
          "type": "string"
        }
      },
      "required": [
        "deck",
        "type",
        "prompt",
        "answer"
      ],
      "type": "object"
    },
    "name": "prep_add_card"
  },
  {
    "description": "Replace a card's editable fields. Pass every field you want to keep — the existing values are NOT merged. Pull current values via prep_get_card first if you want a partial edit.",
    "inputSchema": {
      "properties": {
        "answer": {
          "type": "string"
        },
        "answer_regex": {
          "type": "string"
        },
        "card_id": {
          "type": "integer"
        },
        "choices": {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        "explanation": {
          "type": "string"
        },
        "language": {
          "type": "string"
        },
        "prompt": {
          "type": "string"
        },
        "rubric": {
          "type": "string"
        },
        "skeleton": {
          "type": "string"
        },
        "topic": {
          "type": "string"
        },
        "type": {
          "enum": [
            "short",
            "mcq",
            "multi",
            "code"
          ],
          "type": "string"
        }
      },
      "required": [
        "card_id",
        "type",
        "prompt",
        "answer"
      ],
      "type": "object"
    },
    "name": "prep_update_card"
  },
  {
    "description": "Delete a card. Cascade drops the SRS row + every review for this card. Irreversible.",
    "inputSchema": {
      "properties": {
        "card_id": {
          "type": "integer"
        }
      },
      "required": [
        "card_id"
      ],
      "type": "object"
    },
    "name": "prep_delete_card"
  },
  {
    "description": "Suspend (hide from study sessions) or un-suspend a card. The card keeps its SRS state; suspension just removes it from the due queue.",
    "inputSchema": {
      "properties": {
        "card_id": {
          "type": "integer"
        },
        "suspended": {
          "type": "boolean"
        }
      },
      "required": [
        "card_id",
        "suspended"
      ],
      "type": "object"
    },
    "name": "prep_suspend_card"
  },
  {
    "description": "Render a deck as an Anki .apkg file. Returns a base64-encoded binary; the client can write it to disk and import into Anki, AnkiDroid, or any other consumer.",
    "inputSchema": {
      "properties": {
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name"
      ],
      "type": "object"
    },
    "name": "prep_export_deck_apkg"
  },
  {
    "description": "Import an Anki .apkg file into a deck. Pass the .apkg bytes as a base64-encoded string. Creates the deck if missing. Returns inserted / skipped_duplicates / cloze_skipped / errors.",
    "inputSchema": {
      "properties": {
        "apkg_base64": {
          "description": "Raw .apkg bytes, base64-encoded.",
          "type": "string"
        },
        "name": {
          "type": "string"
        }
      },
      "required": [
        "name",
        "apkg_base64"
      ],
      "type": "object"
    },
    "name": "prep_import_apkg"
  }
];

export const TOOL_NAMES: readonly string[] = TOOLS.map((t) => t.name);
