# 0002. Prepare configuration off the accept loop

The accept loop previously awaited configuration parsing and TLS setup and ran the periodic pool sweep inline. A slow read or a long sweep could delay acceptance after the listeners were ready.

Queue a single reload preparation task on SIGHUP. It parses the file, checks restart-only settings, builds TLS state and compiles the generation. The accept loop publishes a successful result, marks the previous generation draining, and replaces its health checks. An invalid candidate is discarded. SIGHUP notifications can coalesce while preparation is in progress.

Run pool maintenance in one background task and pass published generation numbers through a watch channel. Sweeps treat the received generation as a lower bound because a concurrent sweep can start before a newer generation is published. The existing pool semaphore limits stay authoritative. Shutdown cancels preparation and maintenance scheduling, drains connection tasks, and removes the remaining pooled sockets. The native integration test deliberately stalls a reload candidate and checks fresh data and admin connections while preparation is waiting.
