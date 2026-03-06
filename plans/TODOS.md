# TODOS.md

Just notes of things to do, prompts, etc

## TODOs

use the polymarket api to retrieve all current soccer game
bet $1 in every football game on the 85th minute to whoever is winning

convert this repo to store things in sqlite files instead of json
the json gets out of hand and is slower to load and parse

volatility split strategy

I want to make sure the saved session data is good integrity

-

Brenda's strategy:

in the first 60 seconds, if up or down is 0.28-0.36 (tunable params), buy it
killswitch: if value of position drops 20% sell
if the value of the position rises 30% sell unless there's a gap between tokens of 0.40 (hold in that case)

max_buy_amount and loss_limit_hit are now exposed in tickcontext. Update strategies if relevant
