import java.util.HashMap;
public class Hashmap {
    public static void main(String[] a) {
        int n = 1000000;
        HashMap<String, Long> m = new HashMap<>();
        for (int i = 0; i < n; i++) m.put("key" + i, (long) i);
        long sum = 0, missing = 0;
        for (int i = 0; i < n; i++) { Long v = m.get("key" + i); sum += v == null ? -1 : v; }
        for (int i = 0; i < n; i++) if (!m.containsKey("nokey" + i)) missing++;
        System.out.println(m.size() + " " + sum + " " + missing);
    }
}
