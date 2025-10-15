import sys
from .parse import parse_result

with open(sys.argv[1]) as f:
    game = parse_result(f.read())

for player in game.players:
    print(player)
print(f"{game.victor} won")
if "Town Traitor" in game.modifiers:
    print("Game reached hunt" if game.hunt_reached else "Hunt not reached")
