#include <cstdio>
#include <string>
#include <unordered_map>
int main() {
    long n = 1000000;
    std::unordered_map<std::string, long> m;
    for (long i = 0; i < n; i++) m["key" + std::to_string(i)] = i;
    long sum = 0, missing = 0;
    for (long i = 0; i < n; i++) { auto it = m.find("key" + std::to_string(i)); sum += it == m.end() ? -1 : it->second; }
    for (long i = 0; i < n; i++) if (m.find("nokey" + std::to_string(i)) == m.end()) missing++;
    std::printf("%zu %ld %ld\n", m.size(), sum, missing);
}
