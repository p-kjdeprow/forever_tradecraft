local _, ns = ...

-- Pure lifecycle reducer for the telemetry agent.  Native construction and
-- rendering are adapters around this table, never part of the reducer.
local State = {}
ns.AgentState = State

local TRANSITIONS = {
  unloaded = {
    ordinary = "ordinary", deferred = "deferred", constructing = "constructing",
    restore_only = "restore_only", restore_pending = "restore_pending", failed = "failed",
    recover = "restore_only", restore = "restore_pending",
  },
  ordinary = { defer = "deferred", hold = "ordinary", resume = "ordinary", recover = "restore_only", failed = "failed" },
  deferred = { release = "constructing", recover = "restore_only" },
  constructing = { published = "constructed", failed = "failed" },
  constructed = {
    hold = "constructed", measure = "constructed", resume = "constructed",
    finalize = "finalized", recover = "restore_only", failed = "failed",
  },
  finalized = { recover = "restore_only", defer = "deferred" },
  restore_only = { recover = "restore_only", restore = "restore_pending", failed = "failed" },
  restore_pending = { ready = "ordinary", recover = "restore_only", failed = "failed" },
  failed = { recover = "restore_only", restore = "restore_pending" },
}

local function copy(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for key, child in pairs(value) do result[key] = copy(child) end
  return result
end

function State.New(lifecycle)
  if type(lifecycle) ~= "string" or not TRANSITIONS[lifecycle] then error("agent state lifecycle is invalid") end
  return { lifecycle = lifecycle, nativePhase = "normal", nativeOrdinal = 0, measurementCompleted = false }
end

function State.Transition(current, event)
  if type(current) ~= "table" or type(current.lifecycle) ~= "string" or type(event) ~= "string" then
    error("agent state transition input is malformed")
  end
  local transitions = TRANSITIONS[current.lifecycle]
  local nextLifecycle = transitions and transitions[event]
  if not nextLifecycle then
    return nil, "invalid_transition"
  end
  local nextState = copy(current)
  nextState.lifecycle = nextLifecycle
  if event == "hold" then nextState.nativePhase, nextState.nativeOrdinal = "held", 0 end
  if event == "resume" or event == "ready" then nextState.nativePhase, nextState.nativeOrdinal = "normal", 0 end
  if event == "failed" then nextState.measurementCompleted = false end
  return nextState
end

function State.Measurement(current, ordinal)
  if type(current) ~= "table" or (current.lifecycle ~= "constructed" and current.lifecycle ~= "ordinary") or type(ordinal) ~= "number" or
      ordinal < 0 or ordinal > 100 or ordinal ~= math.floor(ordinal) then
    error("agent state measurement input is malformed")
  end
  local nextState = copy(current)
  nextState.nativePhase = (ordinal == 0 or ordinal == 100) and "held" or "updates"
  nextState.nativeOrdinal = ordinal
  nextState.measurementCompleted = ordinal == 100
  return nextState
end

ns.RegisterModule("AgentState", { requires = { "Core" }, onLoad = function() end })
