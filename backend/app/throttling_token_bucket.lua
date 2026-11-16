-- Atomic Redis Token Bucket using modern HSET
local key = KEYS[1]
local rate = tonumber(ARGV[1])      -- refill rate in tokens/sec
local capacity = tonumber(ARGV[2])  -- burst capacity
local now = tonumber(ARGV[3])       -- current timestamp (seconds float)
local requested = tonumber(ARGV[4]) -- tokens requested (usually 1)

local data = redis.call("HMGET", key, "tokens", "last_updated")
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_updated = now
else
    local delta = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + delta * rate)
    last_updated = now
end

if tokens >= requested then
    tokens = tokens - requested
    redis.call("HSET", key, "tokens", tokens, "last_updated", last_updated)
    redis.call("EXPIRE", key, 3600)
    return {1, 0.0} -- {allowed (1), wait_time_seconds (0)}
else
    local needed = requested - tokens
    local wait_time = needed / rate
    redis.call("HSET", key, "tokens", tokens, "last_updated", last_updated)
    redis.call("EXPIRE", key, 3600)
    return {0, wait_time} -- {denied (0), wait_time_seconds}
end
