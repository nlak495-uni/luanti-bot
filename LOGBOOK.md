After an initial run through of the game I decided that it would be relatively easy for the bot to acquire diamonds if it had access to game info that the player didn't (such as the position of blocks through walls) so in order to make the project more interesting I limited the bot to only being able to perform actions that a human could. To do this I added line of sight checks to all actions and implemented a `look` tool call that used raycasting to determine what the bot could see.

The major challenge with this approach is that it requires a lot more processing and reasoning from the model than simply providing it with all the information required to complete its task, which will result in higher token spend, slower solves and a worse (but more human like) performance.

The model I used was MiniMax-M3 provided by the university connecting to the MCP under opencode, which allowed the model to assess what went wrong and then write the code to fix it, allowing for easier debugging than having a loop that ran the model seperately then providing feedback to the development model in order to improve the server.

Another challenge which presented itself was the architecture of MCP not being amennable to push events such as taking damage, which resulted in the bot sometimes dying while attempting a task and not realising until the current tool call finished. In order to fix this I added interrupting for tasks whenever the bot took damage, which allows the bot to resolve the situation and continue with its task.

One of the tools I fully rewrote was defend, as the model needs to be able to defend itself while performing other tasks, which a seperate defend tool cool can't do. I made defend arm an autoattack mechanism which would run while the model was performing other tasks in order to allow the bot to protect itself.

An issue that occured with the bot relatively consistently was that it spent too much time thinking. In a real time game the bot has limited time to respond to the situation around it, so high token throughput is more valuable than high quality decision making. It could be beneficial to swap out the model used for a small high throughput model but I have neither a subscription to such a model nor the hardware to run one locally so I did not do this.

In order to stop the model wasting so many tokens I added support for getting an alert when a block that was being searched for became visible in order to reduce the number of tool calls used when mining.
