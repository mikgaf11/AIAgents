using UnityEngine;

public class PatrolPoints : MonoBehaviour
{
    public Transform[] patrolPoints;
    public int PointCount => patrolPoints.Length;
    
    public Vector3 GetPatrolPoint(int index)
    {
        return patrolPoints[index].position;
    }
}
