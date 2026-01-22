using Unity.MLAgents;
using Unity.MLAgents.Actuators;
using Unity.MLAgents.Sensors;
using UnityEngine;

public class GladiatorAgent : Agent
{
    public float moveSpeed = 5f;
    public float health = 100f;
    
    private Rigidbody rb;
    
    public override void Initialize()
    {
        rb = GetComponent<Rigidbody>();
    }
    
    public override void OnEpisodeBegin()
    {
        health = 100f;
        transform.position = new Vector3(Random.Range(-10f, 10f), 0.5f, Random.Range(-10f, 10f));
        rb.velocity = Vector3.zero;
    }
    
    public override void CollectObservations(VectorSensor sensor)
    {
        sensor.AddObservation(health / 100f);
        sensor.AddObservation(transform.position.x / 20f);
        sensor.AddObservation(transform.position.z / 20f);
    }
    
    public override void OnActionReceived(ActionBuffers actions)
    {
        float moveX = actions.ContinuousActions[0];
        float moveZ = actions.ContinuousActions[1];
        
        rb.velocity = new Vector3(moveX, 0, moveZ) * moveSpeed;
        
        AddReward(-0.0001f);  // Small penalty per step
        AddReward(health / 1000f);  // Reward for staying alive
    }
}
