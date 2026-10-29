# # Vals harness

## Thoughts
I should support both external and internal harnesses. 
The system should be very modular. In this case, I think the internal harness mode is kind of a degenerate version,
with the preferred approach is making the agent, extenral-local or external-remote, rely on:
1. a sandbox for when its external local
2. a remote server for the client
in EITHER of these cases, the "test chamber" is always a conternarized (unless for some stupid reason disabled) env 
where there is some sort of local, in-chamber proxy for the agent.
that way, this "proxy" gets used by anything, be it external or internal.
the interface should be pretty tight, and i really wonder if the clients (labs) care about the tools the model sees when interfacing with the env.
I should make it completely transparent
I need an **agent adapter** for every kind of agent i work on, OR i could somehow wrap the shell the agent works on (in the case its a sandbox)
with some facade shell that delegates to my proxy interface, which then goes to whatever i plug it into (which is usually the test chamber)

## Data considerations
in the case the model is local to my env, i should also do a cold copy of all the trajectories and logs it produces.
but golden path is to capture the data passed to my proxy and form the trajectory that way. 
- This should get beamed directly to the database.  undecided on how to deal with this: i could store the trajectories locally
  and add some prometheus-like pattern where the storage daemon goes and polls for logs and updates.  
  - i should have two tiers of data: immediate (for observability and monitoring) and cold (for after-the-fact data analysis)
  - unsure on how this might look like.  i might add two channels of data: a fast one and a slower one thats just an archive of the finalized logs etc, and perhaps even a third one, that consolidates or indexes ALL of the artifacts produced by the benchmark i.e grades, logs, etc. 
  - grades themselves get their own tables as well, so this final report is more like a super-index aggregation. 

## Traps
- afaik, the source code has the peculiarity of asking for OS images that do NOT have python.
- this seems on purpose.  we are essentially running an experiment, and so i cannot just add python to their env.


## Interfaces
It is clear we need:
- a “result parser” that is always accompanies the final benchmark spec, which is the spec as provided + our adapters 
- i also need an agent adapter.  it should comprise of a modifier of the inputs that the agent performs e.g if the bash agent needs sh -c but it doesn’t do this by default, then we intercept the signal ourselves as per the agent adapter spec to modify the signals the agent sends to the test chamber. 
- test code is always run in a different, fresh container.  the specs of this container should be defined upfront e.g if we know the tests require python, then we specify so in the spec, and the resulting container should have it.  the agent has absolutely no input into the new container, and by this point the agent container has be winded down with all the data sent.  
* by default, we never include the test code in the test chamber.  
  * the vals spec should first define which are the evaluation directories, and based on that, it will be negated out of the test chamber container, which is where the agent will work. 
  * we also define an agent container spec.  in this case we know the mini swe agent requires bash so we obviosuly include anything it needs e.g if it were claude code then it would contain node etc.
