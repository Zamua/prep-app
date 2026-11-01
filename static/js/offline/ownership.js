// ownership.js: whose offline data this device holds, and the one
// function that changes it. Pure: sync.js reads the state from the
// meta store, applies an event, and performs the effects it gets back.
//
// States:
//   unclaimed  no owner and no wipe: a fresh install, adopted by the
//              next account whose snapshot lands
//   wiped      cleared by a wipe; a row written since belongs to nobody
//   owned      stamped with the account the snapshot came from
//   conflict   this page is signed in as someone else: nothing syncs
//              until the user wipes, and the stores stay that owner's.
//              Held for the page load only; the next load re-derives it.
//
// Events:
//   refreshing  a snapshot refresh is about to fetch
//   flushing    an outbox flush is about to run ({localCards}: how
//               many offline-authored cards are queued)
//   signedIn    the server named this page's account ({user})
//   claim       a snapshot is about to replace the stores
//   stamped     the snapshot landed ({owner}: the record to stamp)
//   wiped       the stores were just cleared
//   kept        the user chose to keep the other account's data
//
// Effects: {type: "stamp", owner}, {type: "dropOrphans"} (rows written
// since a wipe), {type: "clearWiped"}, {type: "recordKeep", userId}.

/** The persisted state, from the `owner` and `wiped` meta records. */
export function ownershipOf({owner, wiped}) {
  if (owner && owner.user_id) return {kind: "owned", owner};
  if (wiped) return {kind: "wiped"};
  return {kind: "unclaimed"};
}

const stay = (state, effects = []) => ({state, effects, refused: null});
const refuse = (state, reason, effects = []) => ({state, effects, refused: reason});

/**
 * Where the device goes from `state` on `event`: {state, effects,
 * refused}. `refused` names why the operation that raised the event
 * must not go on, or is null. Every state and event pair is decided.
 */
export function next(state, event) {
  switch (event.type) {
    case "refreshing":
      return state.kind === "conflict" ? refuse(state, "disabled") : stay(state);
    case "flushing":
      if (state.kind === "conflict") return refuse(state, "disabled");
      if (state.kind === "wiped") return refuse(state, "wiped", [{type: "dropOrphans"}]);
      // An unstamped device cannot say whose outbox this is, so a
      // refresh stamps it first and the cards flush on the next pass.
      if (state.kind === "unclaimed" && event.localCards > 0) return refuse(state, "unstamped");
      return stay(state);
    case "signedIn":
      return signedIn(state, event.user);
    case "claim":
      // From here on, what the queues hold belongs to the account being
      // stamped; anything written since the wipe does not.
      if (state.kind === "wiped") return stay({kind: "unclaimed"}, [{type: "dropOrphans"}, {type: "clearWiped"}]);
      return state.kind === "conflict" ? refuse(state, "disabled") : stay(state);
    case "stamped":
      if (state.kind === "conflict") return refuse(state, "disabled");
      return stay({kind: "owned", owner: event.owner}, [{type: "stamp", owner: event.owner}]);
    case "wiped":
      return stay({kind: "wiped"});
    case "kept":
      if (state.kind !== "conflict") return stay(state);
      return stay(state, [{type: "recordKeep", userId: state.serverUser.id}]);
    default:
      throw new Error("unknown ownership event " + event.type);
  }
}

function signedIn(state, user) {
  const userId = user && user.id;
  switch (state.kind) {
    case "conflict":
      return refuse(state, "disabled");
    case "unclaimed":
    case "wiped":
      return stay(state);
    case "owned": {
      const ownerId = state.owner.user_id;
      if (!userId || ownerId === userId) return stay(state);
      // A merged-away id is this account. The server says so through
      // previous_ids, derived from its own merge audit and never taken
      // from a client; re-stamping makes the next compare plain.
      if (((user && user.previous_ids) || []).includes(ownerId)) {
        const owner = {...state.owner, user_id: userId};
        return stay({kind: "owned", owner}, [{type: "stamp", owner}]);
      }
      return refuse({kind: "conflict", owner: state.owner, serverUser: user}, "mismatch");
    }
    default:
      throw new Error("unknown ownership state " + state.kind);
  }
}

/** Whether to ask the user about a conflict: not when they already chose
 * to keep the data for this same signed-in account. */
export function shouldPrompt(state, dismissed) {
  if (state.kind !== "conflict") return false;
  return !(dismissed && dismissed.dismissed_user_id === state.serverUser.id);
}
