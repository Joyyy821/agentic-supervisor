# Example application: Agentic MAPF

Agentic MAPF is the first application of Agentic Supervisor, not the supervisor's defining purpose. Application-specific objectives, navigation evaluation, and simulator operations remain in the target project and its configuration.

The existing `mas-vln` project has reusable native body-velocity control, a planner subprocess interface, canonical episode execution, trajectory/goal metrics, and CPU-only validation and summary commands. A later integration can invoke those commands and consume their JSON outputs. The reusable supervisor should not import Isaac Sim, ROS, EECBS, or GPU packages.

The application should establish a centralized, language-conditioned multi-robot policy with meaningful coordination and compare it against existing controllers and planner baselines. Existing EECBS integration can be a baseline or component; it does not replace the coordination objective. Scenario definitions and collision/goal/deadlock/constraint metrics belong to the application.

Application roadmap identifiers are `AMAPF-M0` through `AMAPF-M5`. The target repository has unrelated historical milestones; preserve them as-is. M0 supervisor work uses the generic milestone name and does not implement MAPF.
